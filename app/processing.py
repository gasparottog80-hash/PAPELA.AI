"""Bound PDF parsing/OCR in killable, environment-cleared subprocesses.

These child processes are resource-limited but not a security sandbox: a native
exploit could still reach the worker's filesystem or process namespace.
"""

from __future__ import annotations

import json
import multiprocessing
import os
import sys
from multiprocessing.connection import Connection
from typing import Any

from .config import Settings


def _child(pipe: Connection, operation: str, path: str, options: dict[str, Any]):
    # Spawn, not fork: do not inherit database pools, locks or SSH agents.
    os.environ.clear()
    sys.stdout = open(os.devnull, "w")
    sys.stderr = open(os.devnull, "w")
    try:
        if sys.platform != "win32":
            import resource

            memory = options["memory"]
            resource.setrlimit(resource.RLIMIT_AS, (memory, memory))
            cpu = int(options["timeout"]) + 1
            resource.setrlimit(resource.RLIMIT_CPU, (cpu, cpu))
        value: Any
        if operation == "validate":
            from pypdf import PdfReader

            reader = PdfReader(path, strict=True)
            if reader.is_encrypted:
                raise ValueError("encrypted PDF")
            value = len(reader.pages)
        else:
            from papela_fiscal_extractor import extract_fields

            from .ocr import build_engine

            value = build_engine(options["engine"], options["lang"]).extract(path)
            value["fields"] = extract_fields(value)
        data = json.dumps({"ok": True, "value": value}).encode()
        if len(data) > options["max_result"]:
            raise ValueError("result limit")
        pipe.send_bytes(data)
    except Exception:
        # No document fragments, paths, parser messages or stack traces cross IPC.
        pipe.send_bytes(b'{"ok":false}')
    finally:
        pipe.close()


def bounded_process(operation: str, path: str, settings: Settings) -> Any:
    timeout = (
        settings.pdf_timeout_s
        if operation == "validate"
        else settings.processing_timeout_s
    )
    context = multiprocessing.get_context("spawn")
    receiver, sender = context.Pipe(duplex=False)
    child = context.Process(
        target=_child,
        args=(
            sender,
            operation,
            path,
            {
                "memory": settings.processing_memory_bytes,
                "timeout": timeout,
                "max_result": settings.max_result_bytes,
                "engine": settings.ocr_engine,
                "lang": settings.ocr_lang,
            },
        ),
    )
    try:
        child.start()
        sender.close()
        if not receiver.poll(timeout):
            raise TimeoutError("processing deadline")
        try:
            result = json.loads(receiver.recv_bytes(settings.max_result_bytes))
        except (EOFError, OSError, ValueError) as exc:
            raise ValueError("processing rejected") from exc
        if not result.get("ok"):
            raise ValueError("processing rejected")
        return result["value"]
    finally:
        if child.pid is not None:
            if child.is_alive():
                child.terminate()
            child.join(timeout=2)
            if child.is_alive():
                child.kill()
                child.join(timeout=2)
        sender.close()
        receiver.close()
