"""PyWorker generique pour les moteurs audio sans etat d'ABO.

Le proxy ne connait ni job ni utilisateur. Il expose exactement une route du
contrat moteur et la transmet au serveur local ; l'agent relais, lui, porte le
bail ABO et les concessions de media (`ABOB-133`).
"""

import base64
import io
import math
import os
import struct
import wave

from vastai import BenchmarkConfig, HandlerConfig, LogActionConfig, Worker, WorkerConfig

ROUTE = os.environ.get("ABO_SERVERLESS_ROUTE", "")
if ROUTE not in {"/enhance", "/convert"}:
    raise SystemExit("ABO_SERVERLESS_ROUTE doit valoir /enhance ou /convert.")


def _tone(seconds: float = 2.0, rate: int = 16000) -> str:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(rate)
        audio.writeframes(
            b"".join(
                struct.pack("<h", int(6000 * math.sin(index * 0.05)))
                for index in range(int(rate * seconds))
            )
        )
    return base64.b64encode(buffer.getvalue()).decode("ascii")


SAMPLE = _tone()
BENCHMARK = (
    {"audio_b64": SAMPLE, "config": {}}
    if ROUTE == "/enhance"
    else {"audio_b64": SAMPLE, "reference_b64": SAMPLE, "config": {}}
)


def _workload(payload: dict) -> float:
    # Le base64 mesure directement la duree des prises a format comparable.
    # Diviser par quatre garde des nombres maniables sans changer leur ordre.
    return float(
        max(
            (
                len(str(payload.get("audio_b64", "")))
                + len(str(payload.get("reference_b64", "")))
            )
            // 4,
            1,
        )
    )


def build_config() -> WorkerConfig:
    return WorkerConfig(
        model_server_url="http://127.0.0.1",
        model_server_port=18100,
        model_log_file="/var/log/abo-engine.log",
        model_healthcheck_url="/health",
        handlers=[
            HandlerConfig(
                route=ROUTE,
                allow_parallel_requests=False,
                max_queue_time=60.0,
                benchmark_config=BenchmarkConfig(dataset=[BENCHMARK], runs=1),
                workload_calculator=_workload,
            ),
            # Le second temps de la sortie differee (`ADR-016` § 4) : le porteur
            # revient **dans la meme session** avec une concession, et le moteur
            # depose en direct sur R2. Aucun octet ne traverse sa ligne.
            #
            # Sans cette declaration, le PyWorker ne connaitrait pas la route et
            # ne la relaierait pas — le moteur l'expose pourtant, et l'echec
            # arriverait chez un client sans que rien ne l'explique.
            HandlerConfig(
                route="/upload",
                # Un depot n'occupe pas la carte : il n'y a aucune raison de le
                # serialiser derriere un calcul.
                allow_parallel_requests=True,
                max_queue_time=60.0,
                # Charge fixe et faible : ce qui coute est le televersement vers
                # R2, pas le moteur. Le mesurer sur la taille de la charge JSON
                # ferait croire a un travail proportionnel a une URL.
                workload_calculator=lambda _payload: 1.0,
            ),
            # Le filet de nettoyage, appele a la fermeture de session quand
            # `/upload` n'est jamais arrive.
            HandlerConfig(
                route="/drop",
                allow_parallel_requests=True,
                max_queue_time=60.0,
                workload_calculator=lambda _payload: 1.0,
            ),
        ],
        log_action_config=LogActionConfig(
            on_load=["Application startup complete."],
            on_error=[
                "Traceback (most recent call last)",
                "CUDA error",
                "engine failed",
            ],
        ),
    )


if __name__ == "__main__":
    Worker(build_config()).run()
