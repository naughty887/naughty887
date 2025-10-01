#!/usr/bin/env python3
import json
import re
import sys
import time
import uuid
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse
from datetime import datetime, timezone

# In-memory stores guarded by a re-entrant lock for thread safety
lock = threading.RLock()
transactions_by_id = {}
# Maps idempotency key -> {"request_fingerprint": str, "response": bytes, "status": int, "headers": list[(k,v)]}
idempotency_cache = {}


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def json_dumps(data) -> bytes:
    return json.dumps(data, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def parse_body_json(handler: BaseHTTPRequestHandler):
    length_header = handler.headers.get("Content-Length")
    if not length_header:
        return False, (400, {"error": {"type": "invalid_request", "message": "Missing Content-Length header"}})
    try:
        length = int(length_header)
    except ValueError:
        return False, (400, {"error": {"type": "invalid_request", "message": "Invalid Content-Length header"}})

    body = handler.rfile.read(length)
    if not body:
        return False, (400, {"error": {"type": "invalid_request", "message": "Empty request body"}})

    ctype = handler.headers.get("Content-Type", "").split(";")[0].strip().lower()
    if ctype not in ("application/json", "text/json", ""):
        return False, (415, {"error": {"type": "unsupported_media_type", "message": "Content-Type must be application/json"}})

    try:
        data = json.loads(body.decode("utf-8"))
    except json.JSONDecodeError as e:
        return False, (400, {"error": {"type": "invalid_json", "message": f"Malformed JSON: {e.msg}"}})

    return True, data


def validate_transaction_payload(payload: dict):
    required = ["amount", "currency", "source", "destination"]
    missing = [k for k in required if k not in payload]
    if missing:
        return False, (400, {"error": {"type": "invalid_request", "message": f"Missing required fields: {', '.join(missing)}"}})

    # amount validation
    amount = payload["amount"]
    try:
        amount_val = float(amount)
    except (TypeError, ValueError):
        return False, (400, {"error": {"type": "invalid_request", "message": "amount must be a number"}})
    if not (amount_val > 0):
        return False, (400, {"error": {"type": "invalid_request", "message": "amount must be greater than 0"}})

    # currency normalization
    currency = str(payload.get("currency", "")).strip().upper()
    if not re.fullmatch(r"[A-Z]{3}", currency):
        return False, (400, {"error": {"type": "invalid_request", "message": "currency must be a 3-letter ISO code"}})

    # source/destination basic checks
    for field in ("source", "destination"):
        v = str(payload.get(field, "")).strip()
        if not v:
            return False, (400, {"error": {"type": "invalid_request", "message": f"{field} must be a non-empty string"}})

    # sanitized payload
    sanitized = {
        "amount": round(amount_val, 2),
        "currency": currency,
        "source": str(payload["source"]).strip(),
        "destination": str(payload["destination"]).strip(),
        "reference": str(payload.get("reference", "")).strip() or None,
        "metadata": payload.get("metadata") if isinstance(payload.get("metadata"), dict) else None,
    }
    return True, sanitized


def canonical_fingerprint(obj: dict) -> str:
    # Stable fingerprint independent of key order
    return json.dumps(obj, sort_keys=True, separators=(",", ":"))


class JSONHandler(BaseHTTPRequestHandler):
    server_version = "FlashPay/1.0"

    def _set_common_headers(self, status: int, extra_headers=None):
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        # Minimal CORS to ease local testing
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Idempotency-Key")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        if extra_headers:
            for k, v in extra_headers:
                self.send_header(k, v)
        self.end_headers()

    def do_OPTIONS(self):
        self._set_common_headers(204)

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        if path == "/health":
            body = {"status": "ok", "time": utc_now_iso()}
            self._set_common_headers(200)
            self.wfile.write(json_dumps(body))
            return
        if path == "/docs":
            body = {
                "name": "Flash Payment Emulator",
                "version": "1.0",
                "endpoints": {
                    "GET /health": "Health check",
                    "POST /transactions": "Process a flash payment",
                    "GET /transactions/{id}": "Retrieve a processed transaction",
                    "GET /transactions": "List recent transactions"
                }
            }
            self._set_common_headers(200)
            self.wfile.write(json_dumps(body))
            return
        if path == "/transactions":
            with lock:
                items = list(transactions_by_id.values())[-100:]
            self._set_common_headers(200)
            self.wfile.write(json_dumps({"data": items, "has_more": False}))
            return
        m = re.fullmatch(r"/transactions/([A-Za-z0-9_\-]+)", path)
        if m:
            txid = m.group(1)
            with lock:
                txn = transactions_by_id.get(txid)
            if not txn:
                self._set_common_headers(404)
                self.wfile.write(json_dumps({"error": {"type": "not_found", "message": "Transaction not found"}}))
                return
            self._set_common_headers(200)
            self.wfile.write(json_dumps(txn))
            return

        self._set_common_headers(404)
        self.wfile.write(json_dumps({"error": {"type": "not_found", "message": "Unknown path"}}))

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        if path == "/transactions":
            ok, payload_or_error = parse_body_json(self)
            if not ok:
                status, body = payload_or_error
                self._set_common_headers(status)
                self.wfile.write(json_dumps(body))
                return

            payload = payload_or_error
            ok, sanitized_or_error = validate_transaction_payload(payload)
            if not ok:
                status, body = sanitized_or_error
                self._set_common_headers(status)
                self.wfile.write(json_dumps(body))
                return

            sanitized = sanitized_or_error
            idempotency_key = self.headers.get("Idempotency-Key")
            req_fp = canonical_fingerprint(sanitized)

            with lock:
                if idempotency_key and idempotency_key in idempotency_cache:
                    cached = idempotency_cache[idempotency_key]
                    if cached["request_fingerprint"] != req_fp:
                        self._set_common_headers(409)
                        self.wfile.write(json_dumps({
                            "error": {
                                "type": "idempotency_conflict",
                                "message": "This Idempotency-Key has already been used with different request parameters"
                            }
                        }))
                        return
                    self._set_common_headers(cached["status"], cached["headers"])
                    self.wfile.write(cached["response"]) 
                    return

            start_ns = time.perf_counter_ns()

            txn_id = "txn_" + uuid.uuid4().hex
            processed_at = utc_now_iso()

            transaction = {
                "id": txn_id,
                "object": "transaction",
                "status": "succeeded",
                "amount": sanitized["amount"],
                "currency": sanitized["currency"],
                "source": sanitized["source"],
                "destination": sanitized["destination"],
                "reference": sanitized["reference"],
                "metadata": sanitized["metadata"],
                "processed_at": processed_at,
            }

            # Simulate ultra-fast processing by doing minimal work then computing elapsed time
            end_ns = time.perf_counter_ns()
            processing_time_ms = max(0, int((end_ns - start_ns) / 1_000_000))
            transaction["processing_time_ms"] = processing_time_ms

            with lock:
                transactions_by_id[txn_id] = transaction

            response_body = json_dumps(transaction)
            headers = [("Flash-Mode", "true")]

            with lock:
                if idempotency_key:
                    idempotency_cache[idempotency_key] = {
                        "request_fingerprint": req_fp,
                        "response": response_body,
                        "status": 200,
                        "headers": headers,
                    }

            self._set_common_headers(200, extra_headers=headers)
            self.wfile.write(response_body)
            return

        self._set_common_headers(404)
        self.wfile.write(json_dumps({"error": {"type": "not_found", "message": "Unknown path"}}))

    # Reduce default noisy logging
    def log_message(self, fmt, *args):
        sys.stderr.write("[%s] %s\n" % (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), fmt % args))


def run(host="0.0.0.0", port=8080):
    server = ThreadingHTTPServer((host, port), JSONHandler)
    print(f"Flash Payment Emulator listening on http://{host}:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    import os
    host = os.environ.get("BIND", "0.0.0.0")
    try:
        port = int(os.environ.get("PORT", "8080"))
    except ValueError:
        port = 8080
    run(host, port)
