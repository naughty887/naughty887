Flash Payment Emulator

A minimal HTTP service that emulates a flash-fast payment processor. It accepts transactions and returns immediate successful processing responses. Useful for local development and integration testing.

Features
- Ultra-fast transaction processing (in-memory)
- Idempotency support via Idempotency-Key header
- Thread-safe in-memory store
- JSON REST API
- No external dependencies (pure Python standard library)

Requirements
- Python 3.8+

Run
- Default port 8080
- Environment variables: PORT (default 8080), BIND (default 0.0.0.0)

Commands
- Start the server:
  python3 app.py
- Custom port:
  PORT=9090 python3 app.py

API
- Health
  GET /health
  Response: {"status":"ok","time":"..."}

- Process Transaction
  POST /transactions
  Headers:
    Content-Type: application/json
    Idempotency-Key: <optional unique string>
  Body:
  {
    "amount": 25.50,
    "currency": "USD",
    "source": "alice",
    "destination": "bob",
    "reference": "order_123",
    "metadata": {"cart_id": "c_1"}
  }
  200 Response:
  {
    "id": "txn_<...>",
    "object": "transaction",
    "status": "succeeded",
    "amount": 25.5,
    "currency": "USD",
    "source": "alice",
    "destination": "bob",
    "reference": "order_123",
    "metadata": {"cart_id": "c_1"},
    "processed_at": "2025-01-01T00:00:00+00:00",
    "processing_time_ms": 1
  }

- Retrieve Transaction
  GET /transactions/{id}

- List Transactions (last 100)
  GET /transactions

Idempotency
- Supply an Idempotency-Key header to make POST /transactions idempotent.
- Reusing the same key with different request parameters returns 409 conflict.

Examples
- Successful transaction
  curl -s -X POST http://localhost:8080/transactions \
    -H 'Content-Type: application/json' \
    -d '{"amount":12.34,"currency":"USD","source":"alice","destination":"bob","reference":"order_42"}' | jq

- Idempotent repeat
  curl -s -X POST http://localhost:8080/transactions \
    -H 'Content-Type: application/json' \
    -H 'Idempotency-Key: req-123' \
    -d '{"amount":12.34,"currency":"USD","source":"alice","destination":"bob"}' | jq
  curl -s -X POST http://localhost:8080/transactions \
    -H 'Content-Type: application/json' \
    -H 'Idempotency-Key: req-123' \
    -d '{"amount":12.34,"currency":"USD","source":"alice","destination":"bob"}' | jq

Notes
- This emulator keeps data in memory; restarting the server clears all transactions and idempotency cache.
- Do not use in production.
