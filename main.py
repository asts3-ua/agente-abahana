"""
CLI interactivo para el agente de Abahana Villas.

Uso:
    python main.py

Requisitos previos:
    1. gcloud auth application-default login
    2. python create_table.py  (solo la primera vez)
    3. pip install -r requirements.txt
"""

import asyncio
from dotenv import load_dotenv

load_dotenv()  # carga .env antes de que ADK inicialice el cliente Gemini

from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types

from agent import root_agent

APP_NAME = "abahana_villas"
USER_ID = "cliente_01"


async def run():
    session_service = InMemorySessionService()
    session = await session_service.create_session(
        app_name=APP_NAME,
        user_id=USER_ID,
    )

    runner = Runner(
        agent=root_agent,
        app_name=APP_NAME,
        session_service=session_service,
    )

    print("=" * 60)
    print("  Bienvenido al Asistente Virtual de Abahana Villas")
    print("  Costa Blanca — Villas de alquiler vacacional")
    print("=" * 60)
    print("  Escribe 'salir' para terminar.\n")

    while True:
        try:
            user_input = input("Tú: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n¡Hasta pronto!")
            break

        if not user_input:
            continue
        if user_input.lower() in {"salir", "exit", "quit", "q"}:
            print("¡Hasta pronto! Esperamos verle en Abahana Villas.")
            break

        message = types.Content(
            role="user",
            parts=[types.Part(text=user_input)],
        )

        print("\nAgente: ", end="", flush=True)
        async for event in runner.run_async(
            user_id=USER_ID,
            session_id=session.id,
            new_message=message,
        ):
            if event.is_final_response():
                if event.content and event.content.parts:
                    print(event.content.parts[0].text)
        print()


if __name__ == "__main__":
    asyncio.run(run())
