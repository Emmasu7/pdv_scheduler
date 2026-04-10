"""
Módulo principal de planificación de turnos para un Punto de Venta.
Implementa la clase PDVScheduler usando OR-Tools CP-SAT para resolver
la asignación óptima de turnos respetando todas las restricciones del negocio.

Restricciones modeladas:
    R1 — Un solo turno por asesor por día.
    R2 — Cobertura total: cada turno cubierto por exactamente un asesor por día.
    R3 — Consistencia semanal: el asesor mantiene el mismo turno toda la semana.
    R4 — Solo días hábiles: se excluyen domingos y festivos colombianos.
    R5 — (opcional) Rotación semanal: turno distinto en semanas consecutivas.
    R6 — (opcional) Turno fijo para asesor_fijo: solo puede tener APERTURA.
    R7 — (requiere R6) Los otros dos asesores rotan entre CIERRE e INTERMEDIO
         en semanas consecutivas.
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

_TURNO_APERTURA_IDX: int = TURNOS.index("APERTURA")
_TURNO_INTERMEDIO_IDX: int = TURNOS.index("INTERMEDIO")
_TURNO_CIERRE_IDX: int = TURNOS.index("CIERRE")

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
        - No es festivo colombiano según la librería ``holidays``.

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
        aplicar_rotacion: Si ``True``, activa la rotación semanal (R5).
        aplicar_restriccion_asesor_fijo: Si ``True``, activa el turno fijo
            para ``asesor_fijo`` (R6) y la rotación CIERRE/INTERMEDIO
            para los demás (R7).
        asesor_fijo: Nombre del asesor que tendrá APERTURA fija. Obligatorio
            cuando ``aplicar_restriccion_asesor_fijo=True``.

    Raises:
        TypeError: Si las fechas no son instancias de ``datetime.date``.
        ValueError: Si ``asesor_fijo`` no existe en ``ASESORES``, si no hay
            días hábiles en el rango, o si el período no contiene semanas
            válidas.
        RuntimeError: Si el solver CP-SAT no encuentra solución al llamar
            a :meth:`resolver`.
    """

    def __init__(
        self,
        fecha_inicio: date,
        fecha_fin: date,
        aplicar_rotacion: bool = False,
        aplicar_restriccion_asesor_fijo: bool = False,
        asesor_fijo: str | None = None,
    ) -> None:
        """
        Inicializa el planificador, valida los parámetros y construye
        el modelo CP-SAT completo (variables + restricciones).

        Args:
            fecha_inicio: Primer día del período (inclusive).
            fecha_fin: Último día del período (inclusive).
            aplicar_rotacion: Activa la rotación semanal entre semanas (R5).
            aplicar_restriccion_asesor_fijo: Activa el turno fijo (R6) y la
                rotación binaria de los asesores libres (R7).
            asesor_fijo: Nombre del asesor con turno APERTURA fijo. Requerido
                si ``aplicar_restriccion_asesor_fijo=True``.

        Raises:
            ValueError: Si ``aplicar_restriccion_asesor_fijo`` es ``True`` y
                ``asesor_fijo`` es ``None`` o no existe en ``ASESORES``.
            ValueError: Si no hay días hábiles en el rango o el período no
                contiene semanas válidas.
        """
        # ── Validación de parámetros de asesor fijo ──────────────────────────
        if aplicar_restriccion_asesor_fijo:
            if asesor_fijo is None:
                raise ValueError(
                    "Debe especificar 'asesor_fijo' cuando "
                    "'aplicar_restriccion_asesor_fijo' es True."
                )
            if asesor_fijo not in ASESORES:
                raise ValueError(
                    f"El asesor '{asesor_fijo}' no existe en la lista de asesores "
                    f"registrados: {ASESORES}. Verifique el nombre exacto."
                )

        self.fecha_inicio = fecha_inicio
        self.fecha_fin = fecha_fin
        self.aplicar_rotacion = aplicar_rotacion
        self.aplicar_restriccion_asesor_fijo = aplicar_restriccion_asesor_fijo
        self.asesor_fijo = asesor_fijo

        # Índice dinámico del asesor fijo (None si R6 no aplica)
        self._asesor_fijo_idx: int | None = (
            ASESORES.index(asesor_fijo)
            if asesor_fijo is not None
            else None
        )

        # ── Cálculo de días y semanas hábiles ────────────────────────────────
        self.dias_habiles: list[date] = calcular_dias_habiles(
            fecha_inicio, fecha_fin
        )

        if not self.dias_habiles:
            raise ValueError(
                f"No existen días hábiles entre {fecha_inicio} y {fecha_fin}. "
                "Verifique que el rango no corresponda exclusivamente a domingos "
                "y/o festivos colombianos."
            )

        self.semanas: dict[int, list[date]] = _agrupar_por_semana(
            self.dias_habiles
        )

        # ── Validación defensiva de semanas ──────────────────────────────────
        if len(self.semanas) <= 0:
            raise ValueError(
                "El período de planificación no contiene semanas válidas. "
                "Amplíe el rango de fechas."
            )

        # ── Inicialización del modelo CP-SAT ─────────────────────────────────
        self._modelo: cp_model.CpModel = cp_model.CpModel()
        self._solver: cp_model.CpSolver = cp_model.CpSolver()
        self._vars: dict[tuple[int, date, int], cp_model.IntVar] = {}
        self._status: int | None = None
        self._df_resultado: pd.DataFrame | None = None

        self._crear_variables()
        self._agregar_restricciones()

    # ─────────────────────────────────────────────────────────────────────────
    # Construcción del modelo
    # ─────────────────────────────────────────────────────────────────────────

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
        Agrega al modelo todas las restricciones duras en el orden correcto.

        Orden de aplicación:
            1. R1, R2, R3 — siempre activas.
            2. R5 — si ``aplicar_rotacion=True``.
            3. R6 + R7 — si ``aplicar_restriccion_asesor_fijo=True``.

        Raises:
            RuntimeError: Si ocurre un error agregando restricciones.
        """
        try:
            self._r1_un_turno_por_asesor()
            self._r2_cobertura_total()
            self._r3_mismo_turno_semana()

            if self.aplicar_rotacion:
                self._r5_rotacion_semanal()

            if self.aplicar_restriccion_asesor_fijo:
                self._r6_turno_fijo_asesor()
                self._r7_rotacion_asesores_libres()

        except Exception as exc:
            raise RuntimeError(
                f"Error al agregar restricciones al modelo: {exc}"
            ) from exc

    # ─────────────────────────────────────────────────────────────────────────
    # Restricciones base (R1–R3)
    # ─────────────────────────────────────────────────────────────────────────

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

        Implementación: se elige el primer día hábil de cada semana como
        ``dia_ref`` y se fuerza que todos los días restantes de esa semana
        tengan exactamente el mismo valor en cada variable de turno.

        Nota de diseño: Esta restricción es la base sobre la que R5 opera
        correctamente. R5 compara representantes semanales (``dia_ref``),
        y R3 garantiza que esos representantes reflejen la semana completa.

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

    # ─────────────────────────────────────────────────────────────────────────
    # Restricciones opcionales (R5, R6, R7)
    # ─────────────────────────────────────────────────────────────────────────

    def _r5_rotacion_semanal(self) -> None:
        """
        R5 — Un asesor no puede repetir el mismo turno en semanas consecutivas.

        Diseño y relación con R3:
            Esta restricción compara el ``dia_ref`` (primer día hábil) de cada
            semana con el ``dia_ref`` de la semana siguiente. Usar el día
            representante es correcto y suficiente porque R3 garantiza que
            todos los días de una semana tienen el mismo valor de turno.
            Por tanto, ``dia_ref_W != dia_ref_{W+1}`` implica
            ``semana_W != semana_{W+1}`` para el asesor completo.

        Exclusión del asesor fijo:
            Si R6 está activa, el asesor fijo siempre tiene APERTURA, por lo
            que no puede rotar. Se omite para evitar infeasibility.

        Implementación:
            Para cada par de semanas consecutivas (W, W+1) y para cada asesor
            libre, se añade:
                vars[a, dia_ref_W, t] + vars[a, dia_ref_{W+1}, t] <= 1

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
                    # Excluir asesor fijo: siempre tendrá APERTURA (R6)
                    if (
                        self.aplicar_restriccion_asesor_fijo
                        and a_idx == self._asesor_fijo_idx
                    ):
                        continue

                    for t_idx in range(len(TURNOS)):
                        self._modelo.add(
                            self._vars[(a_idx, dia_ref_actual, t_idx)]
                            + self._vars[(a_idx, dia_ref_siguiente, t_idx)]
                            <= 1
                        )
        except Exception as exc:
            raise RuntimeError(
                f"Error al agregar la restricción R5: {exc}"
            ) from exc

    def _r6_turno_fijo_asesor(self) -> None:
        """
        R6 — El asesor designado solo puede trabajar en turno APERTURA.

        Fuerza la variable ``vars[asesor_fijo_idx, dia, APERTURA_IDX] = 1``
        para todos los días hábiles del período. Combinado con R1 y R2,
        esto asegura que los otros dos asesores cubran CIERRE e INTERMEDIO.

        Precondición:
            ``self._asesor_fijo_idx`` no puede ser ``None`` al invocar este
            método. El constructor valida esta condición.

        Raises:
            RuntimeError: Si ocurre un error al agregar la restricción.
        """
        try:
            for dia in self.dias_habiles:
                self._modelo.add(
                    self._vars[
                        (self._asesor_fijo_idx, dia, _TURNO_APERTURA_IDX)
                    ] == 1
                )
        except Exception as exc:
            raise RuntimeError(
                f"Error al agregar la restricción R6: {exc}"
            ) from exc

    def _r7_rotacion_asesores_libres(self) -> None:
        """
        R7 — Con R6 activa, los dos asesores libres rotan entre CIERRE e
        INTERMEDIO en semanas consecutivas.

        Diseño:
            Dado que R6 fija APERTURA al asesor designado y R2 garantiza
            cobertura total, los dos asesores libres solo pueden tener
            CIERRE o INTERMEDIO. R7 hace obligatorio el intercambio:
            si el asesor libre X tiene CIERRE en la semana W, entonces
            en la semana W+1 debe tener INTERMEDIO, y viceversa.

        Implementación (por cada par de semanas consecutivas y asesor libre):
            vars[a, dia_ref_{W+1}, INTERMEDIO] == vars[a, dia_ref_W, CIERRE]
            vars[a, dia_ref_{W+1}, CIERRE]     == vars[a, dia_ref_W, INTERMEDIO]

        Estas dos ecuaciones juntas fuerzan el swap: la variable del turno en
        la semana siguiente es igual a la variable del turno complementario en
        la semana actual.

        Relación con R5:
            R7 implica la no-repetición de R5 para CIERRE/INTERMEDIO. Si R5
            también está activa, sus restricciones sobre APERTURA para asesores
            libres son trivialmente satisfechas (variables siempre 0 por R2+R6).
            No existe contradicción entre R5 y R7.

        Nota:
            Si el período tiene solo 1 semana, no hay semanas consecutivas y
            este método retorna sin añadir restricciones.

        Raises:
            RuntimeError: Si ocurre un error al agregar la restricción.
        """
        try:
            semana_keys = sorted(self.semanas.keys())

            if len(semana_keys) < 2:
                # Sin semanas consecutivas no hay rotación que imponer
                return

            asesores_libres: list[int] = [
                a_idx
                for a_idx in range(len(ASESORES))
                if a_idx != self._asesor_fijo_idx
            ]

            for i in range(len(semana_keys) - 1):
                sem_actual = semana_keys[i]
                sem_siguiente = semana_keys[i + 1]

                dia_ref_actual = self.semanas[sem_actual][0]
                dia_ref_siguiente = self.semanas[sem_siguiente][0]

                for a_idx in asesores_libres:
                    # Semana W tiene CIERRE → semana W+1 debe tener INTERMEDIO
                    self._modelo.add(
                        self._vars[
                            (a_idx, dia_ref_siguiente, _TURNO_INTERMEDIO_IDX)
                        ]
                        == self._vars[
                            (a_idx, dia_ref_actual, _TURNO_CIERRE_IDX)
                        ]
                    )
                    # Semana W tiene INTERMEDIO → semana W+1 debe tener CIERRE
                    self._modelo.add(
                        self._vars[
                            (a_idx, dia_ref_siguiente, _TURNO_CIERRE_IDX)
                        ]
                        == self._vars[
                            (a_idx, dia_ref_actual, _TURNO_INTERMEDIO_IDX)
                        ]
                    )
        except Exception as exc:
            raise RuntimeError(
                f"Error al agregar la restricción R7: {exc}"
            ) from exc

    # ─────────────────────────────────────────────────────────────────────────
    # Resolución y exportación
    # ─────────────────────────────────────────────────────────────────────────

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
                        "Día": _DIAS_ES.get(
                            dia.weekday(), f"Día-{dia.weekday()}"
                        ),
                    }

                    for a_idx, asesor in enumerate(ASESORES):
                        turno_asignado = "N/A"
                        for t_idx, turno in enumerate(TURNOS):
                            if (
                                self._solver.value(
                                    self._vars[(a_idx, dia, t_idx)]
                                ) == 1
                            ):
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

    # ─────────────────────────────────────────────────────────────────────────
    # Propiedades de solo lectura
    # ─────────────────────────────────────────────────────────────────────────

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