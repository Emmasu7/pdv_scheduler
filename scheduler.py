"""
Módulo principal de planificación de turnos para un Punto de Venta.
Implementa la clase PDVScheduler usando OR-Tools CP-SAT para resolver
la asignación óptima de turnos respetando todas las restricciones del negocio.

Restricciones modeladas:
    R1 — Un solo turno por asesor por día.
    R2 — Cobertura total: cada turno cubierto por exactamente un asesor por día.
    R3 — Consistencia semanal: el asesor mantiene el mismo turno toda la semana.
    R4 — Solo días hábiles: se excluyen domingos y festivos colombianos.
    R5 — (automática cuando semanas > 1) Rotación semanal: turno distinto en
         semanas consecutivas.
    R6 — (opcional) Turno fijo para asesor_fijo: solo puede tener ``turno_fijo``.
    R7 — (requiere R6) Los otros dos asesores rotan entre los turnos restantes
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

    La fecha de fin del período se calcula internamente a partir de
    ``fecha_inicio`` y ``semanas``. La rotación semanal (R5) se activa
    de forma automática cuando ``semanas > 1``.

    Args:
        asesores: Lista con los nombres de los asesores del PDV. Debe
            contener exactamente tantos asesores como turnos hay en ``TURNOS``
            (por defecto, 3).
        fecha_inicio: Primer día del período de planificación.
        semanas: Número de semanas a planificar. Determina ``fecha_fin``
            internamente como ``fecha_inicio + timedelta(weeks=semanas) - 1 día``.
            Por defecto 1.
        aplicar_restriccion_asesor_fijo: Si ``True``, activa el turno fijo
            para ``asesor_fijo`` (R6) y la rotación binaria de los asesores
            libres (R7).
        asesor_fijo: Nombre del asesor que tendrá el turno fijo. Obligatorio
            cuando ``aplicar_restriccion_asesor_fijo=True``.
        turno_fijo: Nombre del turno que se asignará de forma fija al
            ``asesor_fijo``. Por defecto ``"APERTURA"``.

    Raises:
        TypeError: Si ``fecha_inicio`` no es instancia de ``datetime.date``.
        ValueError: Si ``asesores`` está vacío o su longitud no coincide con
            ``TURNOS``, si ``turno_fijo`` no existe en ``TURNOS``, si
            ``semanas`` es menor que 1, si ``asesor_fijo`` no existe en
            ``asesores`` cuando R6 está activa, o si no hay días hábiles
            en el rango calculado.
        RuntimeError: Si el solver CP-SAT no encuentra solución al llamar
            a :meth:`resolver`.

    Example::

        scheduler = PDVScheduler(
            asesores=["Ana", "Luis", "María"],
            fecha_inicio=date(2025, 1, 6),
            semanas=2,
            aplicar_restriccion_asesor_fijo=True,
            asesor_fijo="Ana",
            turno_fijo="APERTURA",
        )
        estado = scheduler.resolver()
        df = scheduler.obtener_dataframe()
    """

    def __init__(
        self,
        asesores: list[str],
        fecha_inicio: date,
        semanas: int = 1,
        aplicar_restriccion_asesor_fijo: bool = False,
        asesor_fijo: str | None = None,
        turno_fijo: str = "APERTURA",
    ) -> None:
        """
        Inicializa el planificador, valida los parámetros y construye
        el modelo CP-SAT completo (variables + restricciones).

        Args:
            asesores: Lista de nombres de asesores. Debe tener la misma
                longitud que ``TURNOS``.
            fecha_inicio: Primer día del período (inclusive).
            semanas: Cantidad de semanas a planificar (mínimo 1).
            aplicar_restriccion_asesor_fijo: Activa el turno fijo (R6) y la
                rotación binaria de asesores libres (R7).
            asesor_fijo: Nombre del asesor con turno fijo. Requerido
                si ``aplicar_restriccion_asesor_fijo=True``.
            turno_fijo: Nombre del turno asignado fijo al ``asesor_fijo``.

        Raises:
            TypeError: Si ``fecha_inicio`` no es instancia de ``datetime.date``.
            ValueError: En cualquiera de las siguientes condiciones:
                - ``asesores`` es una lista vacía.
                - ``len(asesores) != len(TURNOS)``.
                - ``turno_fijo`` no pertenece a ``TURNOS``.
                - ``semanas`` es menor que 1.
                - ``aplicar_restriccion_asesor_fijo=True`` y ``asesor_fijo``
                  es ``None`` o no existe en ``asesores``.
                - No hay días hábiles en el rango calculado.
        """
        # ── Validación de tipos ───────────────────────────────────────────────
        if not isinstance(fecha_inicio, date):
            raise TypeError(
                f"'fecha_inicio' debe ser instancia de datetime.date, "
                f"se recibió {type(fecha_inicio).__name__}."
            )

        # ── Validación de asesores ────────────────────────────────────────────
        if not asesores:
            raise ValueError(
                "La lista 'asesores' no puede estar vacía."
            )

        if len(asesores) != len(TURNOS):
            raise ValueError(
                f"La lista 'asesores' debe tener exactamente {len(TURNOS)} "
                f"elementos (uno por turno), pero se recibieron {len(asesores)}: "
                f"{asesores}."
            )

        # ── Validación de turno_fijo ──────────────────────────────────────────
        if turno_fijo not in TURNOS:
            raise ValueError(
                f"El turno_fijo '{turno_fijo}' no es válido. "
                f"Debe ser uno de {TURNOS}."
            )

        # ── Validación de semanas ─────────────────────────────────────────────
        if not isinstance(semanas, int) or semanas < 1:
            raise ValueError(
                f"'semanas' debe ser un entero mayor o igual a 1, "
                f"se recibió: {semanas!r}."
            )

        # ── Validación de asesor fijo ─────────────────────────────────────────
        if aplicar_restriccion_asesor_fijo:
            if asesor_fijo is None:
                raise ValueError(
                    "Debe especificar 'asesor_fijo' cuando "
                    "'aplicar_restriccion_asesor_fijo' es True."
                )
            if asesor_fijo not in asesores:
                raise ValueError(
                    f"El asesor '{asesor_fijo}' no existe en la lista de asesores "
                    f"proporcionada: {asesores}. Verifique el nombre exacto."
                )

        # ── Asignación de atributos públicos ──────────────────────────────────
        self.asesores: list[str] = asesores
        self.fecha_inicio: date = fecha_inicio
        self.num_semanas: int = semanas
        self.fecha_fin: date = fecha_inicio + timedelta(weeks=semanas) - timedelta(days=1)
        self.aplicar_rotacion: bool = semanas > 1  # R5 automática
        self.aplicar_restriccion_asesor_fijo: bool = aplicar_restriccion_asesor_fijo
        self.asesor_fijo: str | None = asesor_fijo
        self.turno_fijo: str = turno_fijo

        # ── Índices dinámicos derivados de los parámetros ────────────────────
        self._turno_fijo_idx: int = TURNOS.index(turno_fijo)

        # Turnos que rotan en R7 (los dos que NO son el turno fijo)
        self._turnos_libres_idx: list[int] = [
            i for i in range(len(TURNOS)) if i != self._turno_fijo_idx
        ]

        # Índice del asesor fijo (None si R6 no aplica)
        self._asesor_fijo_idx: int | None = (
            asesores.index(asesor_fijo)
            if asesor_fijo is not None
            else None
        )

        # ── Cálculo de días y semanas hábiles ─────────────────────────────────
        self.dias_habiles: list[date] = calcular_dias_habiles(
            self.fecha_inicio, self.fecha_fin
        )

        if not self.dias_habiles:
            raise ValueError(
                f"No existen días hábiles entre {self.fecha_inicio} y "
                f"{self.fecha_fin}. Verifique que el rango no corresponda "
                "exclusivamente a domingos y/o festivos colombianos."
            )

        self.semanas: dict[int, list[date]] = _agrupar_por_semana(
            self.dias_habiles
        )

        if len(self.semanas) < 1:
            raise ValueError(
                "El período de planificación no contiene semanas válidas. "
                "Amplíe el rango de fechas."
            )

        # ── Inicialización del modelo CP-SAT ──────────────────────────────────
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
        en ese día. Usa ``self.asesores`` en lugar de la constante global.

        Raises:
            RuntimeError: Si ocurre un error al crear variables del modelo.
        """
        try:
            for a_idx in range(len(self.asesores)):
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
            2. R5 — si ``aplicar_rotacion=True`` (automático cuando ``semanas > 1``).
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
            for a_idx in range(len(self.asesores)):
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
                        for a_idx in range(len(self.asesores))
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
                    for a_idx in range(len(self.asesores)):
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

        Se activa automáticamente cuando ``semanas > 1``. Esta restricción
        compara el ``dia_ref`` (primer día hábil) de cada semana con el de la
        siguiente, lo cual es correcto y suficiente porque R3 garantiza
        uniformidad dentro de cada semana.

        Exclusión del asesor fijo:
            Si R6 está activa, el asesor fijo siempre tiene ``turno_fijo``,
            por lo que no puede rotar. Se omite para evitar infeasibility.

        Implementación:
            Para cada par de semanas consecutivas (W, W+1) y asesor libre::

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

                for a_idx in range(len(self.asesores)):
                    # Excluir asesor fijo: siempre tendrá turno_fijo (R6)
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
        R6 — El asesor designado solo puede trabajar en el turno ``turno_fijo``.

        Fuerza la variable ``vars[asesor_fijo_idx, dia, turno_fijo_idx] = 1``
        para todos los días hábiles del período. Combinado con R1 y R2, esto
        garantiza que los otros dos asesores cubran los turnos restantes.

        El índice del turno se obtiene de ``self._turno_fijo_idx``, que fue
        calculado dinámicamente a partir del parámetro ``turno_fijo``, sin
        depender de ninguna constante hardcodeada.

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
                        (self._asesor_fijo_idx, dia, self._turno_fijo_idx)
                    ] == 1
                )
        except Exception as exc:
            raise RuntimeError(
                f"Error al agregar la restricción R6: {exc}"
            ) from exc

    def _r7_rotacion_asesores_libres(self) -> None:
        """
        R7 — Con R6 activa, los dos asesores libres rotan entre los dos turnos
        restantes (aquellos que no son ``turno_fijo``) en semanas consecutivas.

        Diseño generalizado:
            Los dos índices de turno que rotan están en ``self._turnos_libres_idx``,
            calculados dinámicamente como los índices de ``TURNOS`` distintos a
            ``self._turno_fijo_idx``. Esto permite que R7 funcione correctamente
            sin importar qué valor tenga ``turno_fijo``.

        Implementación (por cada par de semanas consecutivas y asesor libre)::

            vars[a, dia_ref_{W+1}, turno_libre_0] == vars[a, dia_ref_W, turno_libre_1]
            vars[a, dia_ref_{W+1}, turno_libre_1] == vars[a, dia_ref_W, turno_libre_0]

        Estas dos ecuaciones fuerzan el swap: la asignación de turno en la
        semana siguiente es igual a la del turno complementario en la semana actual.

        Nota:
            Si el período tiene solo 1 semana, no hay semanas consecutivas y
            este método retorna sin añadir restricciones.

        Raises:
            RuntimeError: Si ocurre un error al agregar la restricción.
        """
        try:
            semana_keys = sorted(self.semanas.keys())

            if len(semana_keys) < 2:
                return

            asesores_libres: list[int] = [
                a_idx
                for a_idx in range(len(self.asesores))
                if a_idx != self._asesor_fijo_idx
            ]

            # Desempaquetar los dos índices de turno que rotan
            t_libre_0, t_libre_1 = self._turnos_libres_idx

            for i in range(len(semana_keys) - 1):
                sem_actual = semana_keys[i]
                sem_siguiente = semana_keys[i + 1]

                dia_ref_actual = self.semanas[sem_actual][0]
                dia_ref_siguiente = self.semanas[sem_siguiente][0]

                for a_idx in asesores_libres:
                    # Semana W tiene t_libre_0 → semana W+1 debe tener t_libre_1
                    self._modelo.add(
                        self._vars[(a_idx, dia_ref_siguiente, t_libre_1)]
                        == self._vars[(a_idx, dia_ref_actual, t_libre_0)]
                    )
                    # Semana W tiene t_libre_1 → semana W+1 debe tener t_libre_0
                    self._modelo.add(
                        self._vars[(a_idx, dia_ref_siguiente, t_libre_0)]
                        == self._vars[(a_idx, dia_ref_actual, t_libre_1)]
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
        Semana, Fecha, Día y una columna por cada asesor en ``self.asesores``.

        Returns:
            ``pd.DataFrame`` con la planificación completa. Las columnas de
            asesores respetan el orden de ``self.asesores``.

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
            ``pd.DataFrame`` indexado por ``Fecha``, con una columna por
            cada asesor en ``self.asesores``.

        Raises:
            RuntimeError: Si no existe una solución válida para exportar.
        """
        df = self.obtener_dataframe()

        try:
            pivot = df.set_index("Fecha")[self.asesores].copy()
            pivot.index.name = "Fecha"
            return pivot
        except Exception as exc:
            raise RuntimeError(
                f"Error al construir el DataFrame pivoteado: {exc}"
            ) from exc

    def _construir_dataframe(self) -> pd.DataFrame:
        """
        Construye el DataFrame de planificación a partir de la solución del solver.

        Usa ``self.asesores`` para los nombres de columna, en lugar de la
        constante global ``ASESORES``, de modo que el resultado es correcto
        con cualquier lista de asesores pasada al constructor.

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

                    for a_idx, asesor in enumerate(self.asesores):
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
                columns=["Semana", "Fecha", "Día"] + self.asesores,
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

    @property
    def periodo(self) -> str:
        """
        Retorna una cadena descriptiva del período planificado.

        Example::

            "2025-01-06 → 2025-01-19 (2 semanas, 10 días hábiles)"
        """
        return (
            f"{self.fecha_inicio} → {self.fecha_fin} "
            f"({self.num_semanas} semana(s), "
            f"{len(self.dias_habiles)} días hábiles)"
        )


# ─────────────────────────────────────────────────────────────────────────────
# Bloque de prueba
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    """
    Prueba la clase PDVScheduler con dos escenarios reales:
        1. Una semana sin restricción de asesor fijo.
        2. Dos semanas con asesor fijo en APERTURA (R6 + R7 activas).
    """

    ASESORES_PRUEBA = ["Ana", "Luis", "María"]
    FECHA_BASE = date(2025, 1, 6)  # Lunes 6 de enero de 2025

    # ── Escenario 1: 1 semana, sin asesor fijo ────────────────────────────────
    print("=" * 60)
    print("ESCENARIO 1 — 1 semana, sin asesor fijo")
    print("=" * 60)

    try:
        scheduler_1 = PDVScheduler(
            asesores=ASESORES_PRUEBA,
            fecha_inicio=FECHA_BASE,
            semanas=1,
        )
        print(f"Período: {scheduler_1.periodo}")
        print(f"Rotación automática activa: {scheduler_1.aplicar_rotacion}")

        estado_1 = scheduler_1.resolver()
        print(f"Estado solver: {estado_1}")

        if estado_1 in ("OPTIMAL", "FEASIBLE"):
            print(scheduler_1.obtener_dataframe().to_string(index=False))
    except (TypeError, ValueError, RuntimeError) as e:
        print(f"[ERROR] {e}")

    # ── Escenario 2: 2 semanas, con asesor fijo en APERTURA ───────────────────
    print()
    print("=" * 60)
    print("ESCENARIO 2 — 2 semanas, Ana fija en APERTURA (R6 + R7)")
    print("=" * 60)

    try:
        scheduler_2 = PDVScheduler(
            asesores=ASESORES_PRUEBA,
            fecha_inicio=FECHA_BASE,
            semanas=2,
            aplicar_restriccion_asesor_fijo=True,
            asesor_fijo="Ana",
            turno_fijo="APERTURA",
        )
        print(f"Período: {scheduler_2.periodo}")
        print(f"Rotación automática activa: {scheduler_2.aplicar_rotacion}")
        print(f"Turnos libres que rotan: {[TURNOS[i] for i in scheduler_2._turnos_libres_idx]}")

        estado_2 = scheduler_2.resolver()
        print(f"Estado solver: {estado_2}")

        if estado_2 in ("OPTIMAL", "FEASIBLE"):
            print(scheduler_2.obtener_dataframe().to_string(index=False))
            print("\nPivot:")
            print(scheduler_2.obtener_dataframe_pivot().to_string())
    except (TypeError, ValueError, RuntimeError) as e:
        print(f"[ERROR] {e}")

    # ── Escenario 3: Validación de errores esperados ───────────────────────────
    print()
    print("=" * 60)
    print("ESCENARIO 3 — Validaciones de errores")
    print("=" * 60)

    casos_error = [
        {
            "desc": "asesores vacíos",
            "kwargs": {"asesores": [], "fecha_inicio": FECHA_BASE},
        },
        {
            "desc": "turno_fijo inválido",
            "kwargs": {
                "asesores": ASESORES_PRUEBA,
                "fecha_inicio": FECHA_BASE,
                "turno_fijo": "NOCTURNO",
            },
        },
        {
            "desc": "asesor_fijo no en lista",
            "kwargs": {
                "asesores": ASESORES_PRUEBA,
                "fecha_inicio": FECHA_BASE,
                "aplicar_restriccion_asesor_fijo": True,
                "asesor_fijo": "Pedro",
            },
        },
        {
            "desc": "semanas < 1",
            "kwargs": {"asesores": ASESORES_PRUEBA, "fecha_inicio": FECHA_BASE, "semanas": 0},
        },
    ]

    for caso in casos_error:
        try:
            PDVScheduler(**caso["kwargs"])
            print(f"  [{caso['desc']}] → Sin error (inesperado)")
        except (TypeError, ValueError) as e:
            print(f"  [{caso['desc']}] → OK: {e}")