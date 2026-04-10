"""
scheduler.py — PDV Scheduler

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
_ASESOR_FIJO_IDX: int = 0                        # Asesor_1
_TURNO_FIJO_IDX: int  = TURNOS.index("APERTURA") # Turno fijo = APERTURA

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
        - No es domingo  (weekday() != 6).
        - No es festivo colombiano según la librería `holidays`.

    Args:
        fecha_inicio: Primer día del período de planificación (inclusive).
        fecha_fin:    Último día del período de planificación (inclusive).

    Returns:
        Lista ordenada ascendentemente de objetos ``date`` que son días hábiles.

    Raises:
        TypeError:  Si alguno de los argumentos no es instancia de ``datetime.date``.
        ValueError: Si ``fecha_inicio`` es posterior a ``fecha_fin``.

    Ejemplo::

        >>> from datetime import date
        >>> dias = calcular_dias_habiles(date(2025, 1, 6), date(2025, 1, 11))
        >>> len(dias)
        5  # lunes a sábado excluyendo festivos
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

    # Cargar festivos colombianos para todos los años que abarca el rango
    festivos_co = holidays.Colombia(
        years=range(fecha_inicio.year, fecha_fin.year + 1)
    )

    dias_habiles: list[date] = []
    cursor = fecha_inicio

    while cursor <= fecha_fin:
        es_domingo = cursor.weekday() == 6       # 6 = Sunday en Python
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

    # Agrupar por número ISO de semana manteniendo el orden cronológico
    semanas_iso: dict[int, list[date]] = {}
    for dia in dias_habiles:
        clave_iso = dia.isocalendar()[1]          # Número de semana ISO (1–53)
        semanas_iso.setdefault(clave_iso, []).append(dia)

    # Reindexar con 0, 1, 2, … para acceso posicional uniforme
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
        fecha_inicio    : Primer día del período de planificación.
        fecha_fin       : Último día del período de planificación.
        aplicar_r5      : Si ``True``, Asesor_1 solo puede tener turno APERTURA.
        aplicar_rotacion: Si ``True``, activa la rotación semanal (R6).

    Raises:
        TypeError:    Si las fechas no son instancias de ``datetime.date``.
        ValueError:   Si no existen días hábiles en el rango especificado.
        RuntimeError: Si el solver CP-SAT no encuentra solución al llamar
                      a :meth:`resolver`.

    Uso típico::

        from datetime import date
        sched = PDVScheduler(date(2025, 4, 7), date(2025, 4, 12))
        df    = sched.resolver()
        print(df.to_string(index=False))
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
            fecha_inicio    : Primer día del período (inclusive).
            fecha_fin       : Último día del período (inclusive).
            aplicar_r5      : Activa la restricción de turno fijo para Asesor_1.
            aplicar_rotacion: Activa la rotación semanal entre semanas (R6).

        Raises:
            TypeError:  Si las fechas no son instancias de ``datetime.date``.
            ValueError: Si no hay días hábiles en el rango proporcionado.
        """
        self.fecha_inicio     = fecha_inicio
        self.fecha_fin        = fecha_fin
        self.aplicar_r5       = aplicar_r5
        self.aplicar_rotacion = aplicar_rotacion

        # ── Días hábiles y agrupación semanal ──────────────────────────────
        self.dias_habiles: list[date] = calcular_dias_habiles(fecha_inicio, fecha_fin)

        if not self.dias_habiles:
            raise ValueError(
                f"No existen días hábiles entre {fecha_inicio} y {fecha_fin}. "
                "Verifique que el rango no corresponda exclusivamente a domingos "
                "y/o festivos colombianos."
            )

        self.semanas: dict[int, list[date]] = _agrupar_por_semana(self.dias_habiles)

        # ── Modelo y solver CP-SAT ──────────────────────────────────────────
        self._modelo: cp_model.CpModel  = cp_model.CpModel()
        self._solver: cp_model.CpSolver = cp_model.CpSolver()

        # Diccionario de variables booleanas: (asesor_idx, fecha, turno_idx) → BoolVar
        self._vars: dict[tuple[int, date, int], cp_model.IntVar] = {}

        # Estado de la última llamada a resolver()
        self._status: int | None = None

        # DataFrame resultado (disponible tras resolver())
        self._df_resultado: pd.DataFrame | None = None

        # ── Construcción del modelo ─────────────────────────────────────────
        self._crear_variables()
        self._agregar_restricciones()

    # ─────────────────────────────────────────────────────────────────────────
    # Creación de variables booleanas
    # ─────────────────────────────────────────────────────────────────────────

    def _crear_variables(self) -> None:
        """
        Crea las variables booleanas del modelo CP-SAT.

        Para cada tripleta (asesor ``a``, día hábil ``d``, turno ``t``) define::

            vars[a][d][t]  ∈  {0, 1}

        donde el valor 1 significa "el asesor ``a`` trabaja el turno ``t``
        el día ``d``".

        La creación está acotada a los días hábiles calculados (R4 implícita):
        nunca se generan variables para domingos ni festivos.
        """
        for a_idx in range(len(ASESORES)):
            for dia in self.dias_habiles:            # R4: solo días hábiles
                for t_idx in range(len(TURNOS)):
                    nombre_var = f"turno_a{a_idx}_d{dia}_t{t_idx}"
                    self._vars[(a_idx, dia, t_idx)] = (
                        self._modelo.new_bool_var(nombre_var)
                    )

    # ─────────────────────────────────────────────────────────────────────────
    # Orquestador de restricciones
    # ─────────────────────────────────────────────────────────────────────────

    def _agregar_restricciones(self) -> None:
        """
        Agrega al modelo todas las restricciones duras (hard constraints).

        Llama en orden a los métodos privados de cada restricción:
            - R1: Un turno por asesor por día.
            - R2: Cobertura total de turnos por día.
            - R3: Mismo turno durante toda la semana.
            - R5: (condicional) Turno fijo APERTURA para Asesor_1.
            - R6: (condicional) Rotación entre semanas consecutivas.

        R4 es implícita: las variables solo existen para días hábiles.
        """
        self._r1_un_turno_por_asesor()
        self._r2_cobertura_total()
        self._r3_mismo_turno_semana()

        if self.aplicar_r5:
            self._r5_turno_fijo_asesor1()

        if self.aplicar_rotacion:
            self._r6_rotacion_semanal()

    # ─────────────────────────────────────────────────────────────────────────
    # R1 — Un turno por asesor por día
    # ─────────────────────────────────────────────────────────────────────────

    def _r1_un_turno_por_asesor(self) -> None:
        """
        R1 — Exactamente un turno asignado por asesor por día hábil.

        Formulación matemática::

            ∀ a ∈ ASESORES, ∀ d ∈ dias_habiles:
                Σ_{t} vars[a][d][t]  =  1

        Garantiza que un asesor no pueda tener dos turnos simultáneos
        (p. ej., APERTURA y CIERRE en el mismo día).
        """
        for a_idx in range(len(ASESORES)):
            for dia in self.dias_habiles:
                self._modelo.add_exactly_one(
                    self._vars[(a_idx, dia, t_idx)]
                    for t_idx in range(len(TURNOS))
                )

    # ─────────────────────────────────────────────────────────────────────────
    # R2 — Cobertura total
    # ─────────────────────────────────────────────────────────────────────────

    def _r2_cobertura_total(self) -> None:
        """
        R2 — Cada turno debe estar cubierto por exactamente un asesor por día.

        Formulación matemática::

            ∀ t ∈ TURNOS, ∀ d ∈ dias_habiles:
                Σ_{a} vars[a][d][t]  =  1

        Garantiza que los tres turnos (APERTURA, INTERMEDIO, CIERRE) estén
        siempre asignados; ningún asesor puede quedar sin turno ni ningún
        turno puede quedar sin asesor.
        """
        for t_idx in range(len(TURNOS)):
            for dia in self.dias_habiles:
                self._modelo.add_exactly_one(
                    self._vars[(a_idx, dia, t_idx)]
                    for a_idx in range(len(ASESORES))
                )

    # ─────────────────────────────────────────────────────────────────────────
    # R3 — Mismo turno toda la semana
    # ─────────────────────────────────────────────────────────────────────────

    def _r3_mismo_turno_semana(self) -> None:
        """
        R3 — Consistencia semanal: el asesor mantiene el mismo turno toda la semana.

        Estrategia de modelado:
            Se toma el primer día hábil de cada semana como día de referencia ``d0``.
            Para cada día posterior ``d`` de la misma semana se añade la restricción::

                ∀ a ∈ ASESORES, ∀ t ∈ TURNOS, ∀ d ∈ semana[1:]:
                    vars[a][d][t]  ==  vars[a][d0][t]

        Esto asegura que si el lunes al Asesor_1 se le asignó APERTURA,
        todos los demás días hábiles de esa semana el Asesor_1 tendrá APERTURA.

        Nota:
            Semanas de un único día hábil se omiten (la restricción es trivial).
        """
        for sem_idx, dias_semana in self.semanas.items():
            if len(dias_semana) < 2:
                # Una sola jornada: restricción trivialmente satisfecha
                continue

            dia_ref = dias_semana[0]          # Primer día hábil de la semana

            for dia in dias_semana[1:]:
                for a_idx in range(len(ASESORES)):
                    for t_idx in range(len(TURNOS)):
                        self._modelo.add(
                            self._vars[(a_idx, dia, t_idx)]
                            == self._vars[(a_idx, dia_ref, t_idx)]
                        )

    # ─────────────────────────────────────────────────────────────────────────
    # R5 — Turno fijo APERTURA para Asesor_1 (opcional)
    # ─────────────────────────────────────────────────────────────────────────

    def _r5_turno_fijo_asesor1(self) -> None:
        """
        R5 — Restricción especial: Asesor_1 solo puede trabajar en turno APERTURA.

        Fija directamente la variable correspondiente a 1 para cada día hábil::

            ∀ d ∈ dias_habiles:
                vars[_ASESOR_FIJO_IDX][d][_TURNO_FIJO_IDX]  =  1

        El solver redistribuye automáticamente los turnos INTERMEDIO y CIERRE
        entre los asesores restantes (Asesor_2 y Asesor_3).

        Solo se activa si ``aplicar_r5 = True`` en el constructor.
        """
        for dia in self.dias_habiles:
            self._modelo.add(
                self._vars[(_ASESOR_FIJO_IDX, dia, _TURNO_FIJO_IDX)] == 1
            )

    # ─────────────────────────────────────────────────────────────────────────
    # R6 — Rotación semanal (opcional)
    # ─────────────────────────────────────────────────────────────────────────

    def _r6_rotacion_semanal(self) -> None:
        """
        R6 — Rotación semanal: un asesor no puede repetir el mismo turno
        en semanas consecutivas.

        Formulación matemática:
            Para cada par de semanas consecutivas (n, n+1) con días de referencia
            ``d0_n`` y ``d0_{n+1}`` respectivamente::

                ∀ a ∈ ASESORES, ∀ t ∈ TURNOS:
                    vars[a][d0_n][t]  +  vars[a][d0_{n+1}][t]  ≤  1

        Garantiza que si en la semana N el Asesor_2 tuvo CIERRE,
        en la semana N+1 deberá tener APERTURA o INTERMEDIO.

        Nota:
            Si R5 está activa, Asesor_1 queda excluido de esta restricción
            porque su turno ya es fijo (APERTURA toda la semana).

        Solo se activa si ``aplicar_rotacion = True`` en el constructor.
        """
        semana_keys = sorted(self.semanas.keys())

        for i in range(len(semana_keys) - 1):
            sem_actual    = semana_keys[i]
            sem_siguiente = semana_keys[i + 1]

            dia_ref_actual    = self.semanas[sem_actual][0]
            dia_ref_siguiente = self.semanas[sem_siguiente][0]

            for a_idx in range(len(ASESORES)):
                # Asesor con turno fijo (R5) no necesita restricción de rotación
                if self.aplicar_r5 and a_idx == _ASESOR_FIJO_IDX:
                    continue

                for t_idx in range(len(TURNOS)):
                    self._modelo.add(
                        self._vars[(a_idx, dia_ref_actual, t_idx)]
                        + self._vars[(a_idx, dia_ref_siguiente, t_idx)]
                        <= 1
                    )

    # ─────────────────────────────────────────────────────────────────────────
    # Resolución del modelo
    # ─────────────────────────────────────────────────────────────────────────

    def resolver(self) -> pd.DataFrame:
        """
        Ejecuta el solver CP-SAT sobre el modelo construido y retorna
        la planificación resultante como un DataFrame de pandas.

        Returns:
            DataFrame con las columnas:
                - **Semana**  : Número de semana secuencial (1, 2, …).
                - **Fecha**   : Fecha del día en formato ``YYYY-MM-DD``.
                - **Día**     : Nombre del día en español (Lunes, Martes, …).
                - **Asesor_1**: Turno asignado a Asesor_1 ese día.
                - **Asesor_2**: Turno asignado a Asesor_2 ese día.
                - **Asesor_3**: Turno asignado a Asesor_3 ese día.

        Raises:
            RuntimeError: Si el solver retorna INFEASIBLE, UNKNOWN u otro
                          estado que no sea OPTIMAL o FEASIBLE.

        Nota:
            El DataFrame queda almacenado en ``self._df_resultado`` para
            acceso posterior sin necesidad de volver a resolver.
        """
        self._status = self._solver.solve(self._modelo)

        estados_validos = {cp_model.OPTIMAL, cp_model.FEASIBLE}

        if self._status not in estados_validos:
            nombre_estado = self._solver.status_name(self._status)
            raise RuntimeError(
                f"El solver CP-SAT no encontró una solución válida. "
                f"Estado retornado: '{nombre_estado}'. "
                "Posibles causas: restricciones incompatibles, rango de fechas "
                "inválido o modelo mal construido."
            )

        self._df_resultado = self._construir_dataframe()
        return self._df_resultado

    # ─────────────────────────────────────────────────────────────────────────
    # Construcción del DataFrame de resultados
    # ─────────────────────────────────────────────────────────────────────────

    def _construir_dataframe(self) -> pd.DataFrame:
        """
        Construye el DataFrame de planificación a partir de los valores
        de las variables en la solución encontrada por el solver.

        Returns:
            DataFrame con una fila por cada día hábil del período planificado.

        Raises:
            RuntimeError: Si se invoca antes de que el solver haya resuelto
                          el modelo (``_status`` es None).
        """
        if self._status is None:
            raise RuntimeError(
                "Debe llamar a resolver() antes de construir el DataFrame."
            )

        filas: list[dict] = []

        for sem_idx, dias_semana in self.semanas.items():
            for dia in dias_semana:
                fila: dict = {
                    "Semana": sem_idx + 1,
                    "Fecha":  dia.strftime("%Y-%m-%d"),
                    "Día":    _DIAS_ES.get(dia.weekday(), f"Día-{dia.weekday()}"),
                }

                for a_idx, asesor in enumerate(ASESORES):
                    turno_asignado = "N/A"
                    for t_idx, turno in enumerate(TURNOS):
                        if self._solver.value(self._vars[(a_idx, dia, t_idx)]) == 1:
                            turno_asignado = turno
                            break
                    fila[asesor] = turno_asignado

                filas.append(fila)

        df = pd.DataFrame(filas, columns=["Semana", "Fecha", "Día"] + ASESORES)
        return df

    # ─────────────────────────────────────────────────────────────────────────
    # Propiedades de sólo lectura
    # ─────────────────────────────────────────────────────────────────────────

    @property
    def resultado(self) -> pd.DataFrame | None:
        """Retorna el DataFrame de resultados si el modelo fue resuelto, o None."""
        return self._df_resultado

    @property
    def estado_solver(self) -> str | None:
        """Retorna el nombre del estado del solver tras la última ejecución."""
        if self._status is None:
            return None
        return self._solver.status_name(self._status)