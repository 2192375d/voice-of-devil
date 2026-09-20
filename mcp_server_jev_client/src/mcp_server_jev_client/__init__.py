def main() -> None:
    import logging

    import uvicorn

    logging.basicConfig(level=logging.INFO)
    # One worker owns the single shared game's observation pipeline.
    uvicorn.run("mcp_server_jev_client.app:app", host="127.0.0.1", port=8000, workers=1)
