"""
Módulo principal de planificación de turnos para un Punto de Venta.
Implementa la clase PDVScheduler usando OR-Tools CP-SAT para resolver
la asignación óptima de turnos respetando todas las restricciones del negocio.

Restricciones modeladas:
    R1 — Un solo turno por asesor por día.
    R2 — Cobertura total: cada turno cubierto por exactamente un asesor por día.
    R3 — Consistencia semanal: el asesor mantiene el mismo turno toda la semana.
    R4 — Solo días hábiles: se excluyen domingos y festivos colombianos.
    R5 — (opcional) Turno fijo para Asesor_1: solo puede tener APERTURA.
    R6 — (opcional) Rotación semanal: turno distinto en semanas consecutivas.
"""

from __future__ import annotations

import holidays
import pandas as pd
from datetime import date, timedelta
from ortools.sat.python import cp_model


# ─────────────────────────────────────────────────────────────────────────────
# Constantes del dominio
# ─────────────────────────────────────────────────────────────────────────────

TURNOS: list[str] = ["APERTURA", "INTERMEDIO", "CIERRE"]
"""Lista de los tres tipos de turno disponibles en el PDV."""

ASESORES: list[str] = ["Asesor_1", "Asesor_2", "Asesor_3"]
"""Lista de los tres asesores del punto de venta."""

# Índices para la restricción especial R5
_ASESOR_FIJO_IDX: int = 0
_TURNO_FIJO_IDX: int = TURNOS.index("APERTURA")

# Nombres de días en español para el DataFrame final
_DIAS_ES: dict[int, str] = {
    0: "Lunes",
    1: "Martes",
    2: "Miércoles",
    3: "Jueves",
    4: "Viernes",
    5: "Sábado",
}


# ─────────────────────────────────────────────────────────────────────────────
# Funciones auxiliares
# ─────────────────────────────────────────────────────────────────────────────

def calcular_dias_habiles(
    fecha_inicio: date,
    fecha_fin: date,
) -> list[date]:
    """
    Calcula los días hábiles dentro del rango [fecha_inicio, fecha_fin].

    Un día es hábil si cumple ambas condiciones:
        - No es domingo (weekday() != 6).
        - No es festivo colombiano según la librería `holidays`.

    Args:
        fecha_inicio: Primer día del período de planificación (inclusive).
        fecha_fin: Último día del período de planificación (inclusive).

    Returns:
        Lista ordenada ascendentemente de objetos ``date`` que son días hábiles.

    Raises:
        TypeError: Si alguno de los argumentos no es instancia de ``datetime.date``.
        ValueError: Si ``fecha_inicio`` es posterior a ``fecha_fin``.
    """
    if not isinstance(fecha_inicio, date) or not isinstance(fecha_fin, date):
        raise TypeError(
            "Los argumentos 'fecha_inicio' y 'fecha_fin' deben ser instancias "
            "de datetime.date."
        )

    if fecha_inicio > fecha_fin:
        raise ValueError(
            f"'fecha_inicio' ({fecha_inicio}) no puede ser posterior a "
            f"'fecha_fin' ({fecha_fin})."
        )

    festivos_co = holidays.Colombia(
        years=range(fecha_inicio.year, fecha_fin.year + 1)
    )

    dias_habiles: list[date] = []
    cursor = fecha_inicio

    while cursor <= fecha_fin:
        es_domingo = cursor.weekday() == 6
        es_festivo = cursor in festivos_co

        if not es_domingo and not es_festivo:
            dias_habiles.append(cursor)

        cursor += timedelta(days=1)

    return dias_habiles


def _agrupar_por_semana(dias_habiles: list[date]) -> dict[int, list[date]]:
    """
    Agrupa los días hábiles por semana ISO, reindexando con clave secuencial.

    El resultado usa claves 0, 1, 2, … para facilitar la iteración por índice
    en las restricciones del modelo.

    Args:
        dias_habiles: Lista ordenada de días hábiles (sin domingos ni festivos).

    Returns:
        Diccionario ``{semana_idx: [fecha, ...]}`` ordenado cronológicamente,
        donde ``semana_idx`` empieza en 0.

    Raises:
        ValueError: Si la lista de días hábiles está vacía.
    """
    if not dias_habiles:
        raise ValueError(
            "La lista de días hábiles está vacía. No es posible agrupar por semana."
        )

    semanas_iso: dict[int, list[date]] = {}
    for dia in dias_habiles:
        clave_iso = dia.isocalendar()[1]
        semanas_iso.setdefault(clave_iso, []).append(dia)

    semanas_seq: dict[int, list[date]] = {
        idx: dias_sem
        for idx, dias_sem in enumerate(semanas_iso.values())
    }

    return semanas_seq


# ─────────────────────────────────────────────────────────────────────────────
# Clase principal PDVScheduler
# ─────────────────────────────────────────────────────────────────────────────

class PDVScheduler:
    """
    Planificador de turnos para un Punto de Venta (PDV) con CP-SAT.

    Encapsula la creación del modelo de programación por restricciones,
    la definición de variables booleanas, la adición de restricciones duras
    y la resolución del problema de asignación de turnos.

    Args:
        fecha_inicio: Primer día del período de planificación.
        fecha_fin: Último día del período de planificación.
        aplicar_r5: Si ``True``, Asesor_1 solo puede tener turno APERTURA.
        aplicar_rotacion: Si ``True``, activa la rotación semanal (R6).

    Raises:
        TypeError: Si las fechas no son instancias de ``datetime.date``.
        ValueError: Si no existen días hábiles en el rango especificado.
        RuntimeError: Si el solver CP-SAT no encuentra solución al llamar
            a :meth:`resolver`.
    """

    def __init__(
        self,
        fecha_inicio: date,
        fecha_fin: date,
        aplicar_r5: bool = False,
        aplicar_rotacion: bool = False,
    ) -> None:
        """
        Inicializa el planificador, valida el rango de fechas y construye
        el modelo CP-SAT completo (variables + restricciones).

        Args:
            fecha_inicio: Primer día del período (inclusive).
            fecha_fin: Último día del período (inclusive).
            aplicar_r5: Activa la restricción de turno fijo para Asesor_1.
            aplicar_rotacion: Activa la rotación semanal entre semanas (R6).

        Raises:
            TypeError: Si las fechas no son instancias de ``datetime.date``.
            ValueError: Si no hay días hábiles en el rango proporcionado.
        """
        self.fecha_inicio = fecha_inicio
        self.fecha_fin = fecha_fin
        self.aplicar_r5 = aplicar_r5
        self.aplicar_rotacion = aplicar_rotacion

        self.dias_habiles: list[date] = calcular_dias_habiles(fecha_inicio, fecha_fin)

        if not self.dias_habiles:
            raise ValueError(
                f"No existen días hábiles entre {fecha_inicio} y {fecha_fin}. "
                "Verifique que el rango no corresponda exclusivamente a domingos "
                "y/o festivos colombianos."
            )

        self.semanas: dict[int, list[date]] = _agrupar_por_semana(self.dias_habiles)

        self._modelo: cp_model.CpModel = cp_model.CpModel()
        self._solver: cp_model.CpSolver = cp_model.CpSolver()
        self._vars: dict[tuple[int, date, int], cp_model.IntVar] = {}
        self._status: int | None = None
        self._df_resultado: pd.DataFrame | None = None

        self._crear_variables()
        self._agregar_restricciones()

    def _crear_variables(self) -> None:
        """
        Crea las variables booleanas del modelo CP-SAT.

        Para cada tripleta (asesor ``a``, día hábil ``d``, turno ``t``) define
        una variable booleana que vale 1 cuando el asesor trabaja ese turno
        en ese día.

        Raises:
            RuntimeError: Si ocurre un error al crear variables del modelo.
        """
        try:
            for a_idx in range(len(ASESORES)):
                for dia in self.dias_habiles:
                    for t_idx in range(len(TURNOS)):
                        nombre_var = f"turno_a{a_idx}_d{dia}_t{t_idx}"
                        self._vars[(a_idx, dia, t_idx)] = (
                            self._modelo.new_bool_var(nombre_var)
                        )
        except Exception as exc:
            raise RuntimeError(
                f"Error al crear las variables del modelo: {exc}"
            ) from exc

    def _agregar_restricciones(self) -> None:
        """
        Agrega al modelo todas las restricciones duras.

        Raises:
            RuntimeError: Si ocurre un error agregando restricciones.
        """
        try:
            self._r1_un_turno_por_asesor()
            self._r2_cobertura_total()
            self._r3_mismo_turno_semana()

            if self.aplicar_r5:
                self._r5_turno_fijo_asesor1()

            if self.aplicar_rotacion:
                self._r6_rotacion_semanal()
        except Exception as exc:
            raise RuntimeError(
                f"Error al agregar restricciones al modelo: {exc}"
            ) from exc

    def _r1_un_turno_por_asesor(self) -> None:
        """
        R1 — Exactamente un turno asignado por asesor por día hábil.

        Raises:
            RuntimeError: Si ocurre un error al agregar la restricción.
        """
        try:
            for a_idx in range(len(ASESORES)):
                for dia in self.dias_habiles:
                    self._modelo.add_exactly_one(
                        self._vars[(a_idx, dia, t_idx)]
                        for t_idx in range(len(TURNOS))
                    )
        except Exception as exc:
            raise RuntimeError(
                f"Error al agregar la restricción R1: {exc}"
            ) from exc

    def _r2_cobertura_total(self) -> None:
        """
        R2 — Cada turno debe estar cubierto por exactamente un asesor por día.

        Raises:
            RuntimeError: Si ocurre un error al agregar la restricción.
        """
        try:
            for t_idx in range(len(TURNOS)):
                for dia in self.dias_habiles:
                    self._modelo.add_exactly_one(
                        self._vars[(a_idx, dia, t_idx)]
                        for a_idx in range(len(ASESORES))
                    )
        except Exception as exc:
            raise RuntimeError(
                f"Error al agregar la restricción R2: {exc}"
            ) from exc

    def _r3_mismo_turno_semana(self) -> None:
        """
        R3 — Consistencia semanal: el asesor mantiene el mismo turno toda la semana.

        Raises:
            RuntimeError: Si ocurre un error al agregar la restricción.
        """
        try:
            for _, dias_semana in self.semanas.items():
                if len(dias_semana) < 2:
                    continue

                dia_ref = dias_semana[0]

                for dia in dias_semana[1:]:
                    for a_idx in range(len(ASESORES)):
                        for t_idx in range(len(TURNOS)):
                            self._modelo.add(
                                self._vars[(a_idx, dia, t_idx)]
                                == self._vars[(a_idx, dia_ref, t_idx)]
                            )
        except Exception as exc:
            raise RuntimeError(
                f"Error al agregar la restricción R3: {exc}"
            ) from exc

    def _r5_turno_fijo_asesor1(self) -> None:
        """
        R5 — Asesor_1 solo puede trabajar en turno APERTURA.

        Raises:
            RuntimeError: Si ocurre un error al agregar la restricción.
        """
        try:
            for dia in self.dias_habiles:
                self._modelo.add(
                    self._vars[(_ASESOR_FIJO_IDX, dia, _TURNO_FIJO_IDX)] == 1
                )
        except Exception as exc:
            raise RuntimeError(
                f"Error al agregar la restricción R5: {exc}"
            ) from exc

    def _r6_rotacion_semanal(self) -> None:
        """
        R6 — Un asesor no puede repetir el mismo turno en semanas consecutivas.

        Raises:
            RuntimeError: Si ocurre un error al agregar la restricción.
        """
        try:
            semana_keys = sorted(self.semanas.keys())

            for i in range(len(semana_keys) - 1):
                sem_actual = semana_keys[i]
                sem_siguiente = semana_keys[i + 1]

                dia_ref_actual = self.semanas[sem_actual][0]
                dia_ref_siguiente = self.semanas[sem_siguiente][0]

                for a_idx in range(len(ASESORES)):
                    if self.aplicar_r5 and a_idx == _ASESOR_FIJO_IDX:
                        continue

                    for t_idx in range(len(TURNOS)):
                        self._modelo.add(
                            self._vars[(a_idx, dia_ref_actual, t_idx)]
                            + self._vars[(a_idx, dia_ref_siguiente, t_idx)]
                            <= 1
                        )
        except Exception as exc:
            raise RuntimeError(
                f"Error al agregar la restricción R6: {exc}"
            ) from exc

    def resolver(self) -> str:
        """
        Ejecuta el solver CP-SAT y retorna el estado de la solución.

        Returns:
            ``"OPTIMAL"``, ``"FEASIBLE"`` o ``"INFEASIBLE"``.

        Raises:
            RuntimeError: Si el solver falla o retorna un estado inesperado.
        """
        try:
            self._status = self._solver.solve(self._modelo)
        except Exception as exc:
            raise RuntimeError(
                f"El solver CP-SAT lanzó una excepción inesperada: {exc}"
            ) from exc

        if self._status == cp_model.OPTIMAL:
            self._df_resultado = self._construir_dataframe()
            return "OPTIMAL"

        if self._status == cp_model.FEASIBLE:
            self._df_resultado = self._construir_dataframe()
            return "FEASIBLE"

        if self._status == cp_model.INFEASIBLE:
            return "INFEASIBLE"

        nombre_estado = self._solver.status_name(self._status)
        raise RuntimeError(
            f"El solver terminó con un estado no manejable: '{nombre_estado}'. "
            "Revise que las restricciones del modelo sean consistentes."
        )

    def obtener_dataframe(self) -> pd.DataFrame:
        """
        Retorna el DataFrame de planificación con columnas:
        Semana, Fecha, Día y una columna por asesor.

        Returns:
            ``pd.DataFrame`` con la planificación completa.

        Raises:
            RuntimeError: Si no existe una solución válida para exportar.
        """
        self._verificar_solucion_disponible()
        return self._df_resultado.copy()

    def obtener_dataframe_pivot(self) -> pd.DataFrame:
        """
        Retorna la planificación pivoteada con fechas como filas
        y asesores como columnas.

        Returns:
            ``pd.DataFrame`` pivoteado.

        Raises:
            RuntimeError: Si no existe una solución válida para exportar.
        """
        df = self.obtener_dataframe()

        try:
            pivot = df.set_index("Fecha")[ASESORES].copy()
            pivot.index.name = "Fecha"
            return pivot
        except Exception as exc:
            raise RuntimeError(
                f"Error al construir el DataFrame pivoteado: {exc}"
            ) from exc

    def _construir_dataframe(self) -> pd.DataFrame:
        """
        Construye el DataFrame de planificación a partir de la solución del solver.

        Returns:
            DataFrame con una fila por cada día hábil del período planificado.

        Raises:
            RuntimeError: Si se invoca antes de resolver el modelo o si falla
                la construcción del DataFrame.
        """
        if self._status is None:
            raise RuntimeError(
                "Debe llamar a resolver() antes de construir el DataFrame."
            )

        try:
            filas: list[dict] = []

            for sem_idx, dias_semana in self.semanas.items():
                for dia in dias_semana:
                    fila: dict = {
                        "Semana": sem_idx + 1,
                        "Fecha": dia.strftime("%Y-%m-%d"),
                        "Día": _DIAS_ES.get(dia.weekday(), f"Día-{dia.weekday()}"),
                    }

                    for a_idx, asesor in enumerate(ASESORES):
                        turno_asignado = "N/A"
                        for t_idx, turno in enumerate(TURNOS):
                            if self._solver.value(self._vars[(a_idx, dia, t_idx)]) == 1:
                                turno_asignado = turno
                                break
                        fila[asesor] = turno_asignado

                    filas.append(fila)

            return pd.DataFrame(
                filas,
                columns=["Semana", "Fecha", "Día"] + ASESORES,
            )
        except Exception as exc:
            raise RuntimeError(
                f"Error al construir el DataFrame de resultados: {exc}"
            ) from exc

    def _verificar_solucion_disponible(self) -> None:
        """
        Valida que exista una solución lista para exportar.

        Raises:
            RuntimeError: Si resolver() no fue llamado o si el modelo no tiene
                solución válida.
        """
        if self._status is None:
            raise RuntimeError(
                "No hay resultados disponibles. Llame a resolver() primero."
            )

        if self._status not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
            nombre_estado = self._solver.status_name(self._status)
            raise RuntimeError(
                f"No existe solución válida para exportar. "
                f"Estado del solver: '{nombre_estado}'."
            )

    @property
    def resultado(self) -> pd.DataFrame | None:
        """
        Retorna el DataFrame de resultados si el modelo fue resuelto, o ``None``.
        """
        return self._df_resultado

    @property
    def estado_solver(self) -> str | None:
        """
        Retorna el nombre del estado del solver tras la última ejecución.
        """
        if self._status is None:
            return None
        return self._solver.status_name(self._status)