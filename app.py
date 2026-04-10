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
        turno_fijo_param = TURNO_FIJO if aplicar_restriccion else None
        asesor_fijo_param = ASESOR_FIJO if aplicar_restriccion else None

        scheduler = PDVScheduler(
            asesores=ASESORES,
            fecha_inicio=fecha_inicio,
            semanas=semanas,
            turno_fijo=turno_fijo_param,
            asesor_fijo=asesor_fijo_param,
        )

        # ── Ejecución del solver ──────────────────────────────────────────────
        df = scheduler.planificar()

        if df is None or df.empty:
            raise RuntimeError(
                "El solver no encontró una solución factible para los parámetros indicados. "
                "Intenta con otra fecha de inicio o verifica que existan días hábiles en el período."
            )

        # ── Preparación de datos para la plantilla ────────────────────────────
        # Construir estructura: lista de dicts por fila
        registros = df.to_dict(orient="records")

        # Calcular resumen
        fecha_fin = fecha_inicio + timedelta(weeks=semanas) - timedelta(days=1)
        total_dias_habiles = len(df["Fecha"].unique()) if "Fecha" in df.columns else len(df)

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
        # Errores esperados: parámetros inválidos o solver infactible
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
        # Errores inesperados: mostrar mensaje genérico sin exponer trazas internas
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