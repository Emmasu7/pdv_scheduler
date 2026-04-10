"""
Módulo principal de la aplicación Flask para el PDV Scheduler.
Expone dos rutas: GET / (formulario) y POST /planificar (ejecución del solver).
"""

from flask import Flask, render_template, request
from datetime import date, timedelta

from scheduler import PDVScheduler

app = Flask(__name__)

# ─── Constantes ───────────────────────────────────────────────────────────────
ASESORES = ["Asesor_1", "Asesor_2", "Asesor_3"]
ASESOR_FIJO = "Asesor_1"
TURNO_FIJO = "APERTURA"

ESTADOS_VALIDOS = {"OPTIMAL", "FEASIBLE"}


def _lunes_semana_actual() -> str:
    """Retorna la fecha del lunes de la semana actual en formato ISO (YYYY-MM-DD)."""
    hoy = date.today()
    lunes = hoy - timedelta(days=hoy.weekday())
    return lunes.isoformat()


# ─── Rutas ────────────────────────────────────────────────────────────────────

@app.route("/", methods=["GET"])
def index():
    """
    Renderiza la página principal con el formulario de planificación.
    El campo fecha_inicio se pre-rellena con el lunes de la semana actual.
    """
    fecha_default = _lunes_semana_actual()
    return render_template("index.html", fecha_default=fecha_default)


@app.route("/planificar", methods=["POST"])
def planificar():
    """
    Recibe el formulario, instancia PDVScheduler y ejecuta el solver.
    Flujo:
      1. scheduler.resolver()        → ejecuta CP-SAT, retorna estado string
      2. scheduler.obtener_dataframe() → retorna DataFrame con la planificación
    Renderiza resultado.html con la tabla de turnos o un mensaje de error amigable.
    """
    try:
        # ── Lectura y validación de parámetros del formulario ─────────────────
        fecha_inicio_str = request.form.get("fecha_inicio", "").strip()
        semanas_str = request.form.get("semanas", "1").strip()
        aplicar_restriccion = request.form.get("aplicar_restriccion") == "on"

        if not fecha_inicio_str:
            raise ValueError("Debes seleccionar una fecha de inicio.")

        try:
            fecha_inicio = date.fromisoformat(fecha_inicio_str)
        except ValueError:
            raise ValueError(f"La fecha '{fecha_inicio_str}' no tiene un formato válido (YYYY-MM-DD).")

        if semanas_str not in ("1", "4"):
            raise ValueError("El número de semanas debe ser 1 o 4.")
        semanas = int(semanas_str)

        # ── Construcción del scheduler ────────────────────────────────────────
        if aplicar_restriccion:
            scheduler = PDVScheduler(
                asesores=ASESORES,
                fecha_inicio=fecha_inicio,
                semanas=semanas,
                turno_fijo=TURNO_FIJO,
                asesor_fijo=ASESOR_FIJO,
            )
        else:
            scheduler = PDVScheduler(
                asesores=ASESORES,
                fecha_inicio=fecha_inicio,
                semanas=semanas,
            )

        # ── Ejecución del solver ──────────────────────────────────────────────
        estado = scheduler.resolver()

        if estado not in ESTADOS_VALIDOS:
            raise RuntimeError(
                f"El solver no encontró una solución factible (estado: {estado}). "
                "Intenta con otra fecha de inicio o verifica que existan días hábiles en el período."
            )

        # ── Obtención del DataFrame ───────────────────────────────────────────
        df = scheduler.obtener_dataframe()

        if df is None or df.empty:
            raise RuntimeError(
                "El solver resolvió correctamente pero no generó registros. "
                "Verifica que el período seleccionado contenga días hábiles."
            )

        # ── Preparación de datos para la plantilla ────────────────────────────
        registros = df.to_dict(orient="records")
        fecha_fin = fecha_inicio + timedelta(weeks=semanas) - timedelta(days=1)
        total_dias_habiles = df["Fecha"].nunique() if "Fecha" in df.columns else len(df)

        return render_template(
            "resultado.html",
            registros=registros,
            columnas=list(df.columns),
            fecha_inicio=fecha_inicio.strftime("%d/%m/%Y"),
            fecha_fin=fecha_fin.strftime("%d/%m/%Y"),
            aplicar_restriccion=aplicar_restriccion,
            asesor_fijo=ASESOR_FIJO,
            turno_fijo=TURNO_FIJO,
            total_dias_habiles=total_dias_habiles,
            error=None,
        )

    except (ValueError, RuntimeError) as exc:
        return render_template(
            "resultado.html",
            registros=[],
            columnas=[],
            fecha_inicio="—",
            fecha_fin="—",
            aplicar_restriccion=False,
            asesor_fijo=ASESOR_FIJO,
            turno_fijo=TURNO_FIJO,
            total_dias_habiles=0,
            error=str(exc),
        )
    except Exception as exc:
        return render_template(
            "resultado.html",
            registros=[],
            columnas=[],
            fecha_inicio="—",
            fecha_fin="—",
            aplicar_restriccion=False,
            asesor_fijo=ASESOR_FIJO,
            turno_fijo=TURNO_FIJO,
            total_dias_habiles=0,
            error=f"Error inesperado: {exc}",
        )


# ─── Punto de entrada ─────────────────────────────────────────────────────────

if __name__ == "__main__":
    app.run(debug=True)