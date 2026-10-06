# Distributed System with JWT and TCC

A microservices-based distributed order management system built with **FastAPI**, **JWT authentication**, **Docker Compose**, the **Try-Confirm/Cancel (TCC)** transaction pattern, and real card payments through the **Stripe sandbox**.

The project demonstrates how a coordinator can manage a distributed business operation across independent services while handling partial failures, compensation, idempotent operations, persistent decisions, retries, transaction recovery, and time-bounded reservations through a configurable TTL.

The payment participant is backed by the **Stripe test environment**: Try authorizes the amount on a test card, Confirm captures it, and Cancel releases the authorization. A small **web client** (`frontend/`) shows the whole flow in the browser.

---

## Table of Contents

- [Overview](#overview)
- [Goals](#goals)
- [Architecture](#architecture)
- [Services](#services)
- [TCC Pattern](#tcc-pattern)
- [Stripe Sandbox Integration](#stripe-sandbox-integration)
- [Web Client](#web-client)
- [Transaction Decision and Status](#transaction-decision-and-status)
- [Distributed Order Flow](#distributed-order-flow)
- [Idempotency and Empty Cancel](#idempotency-and-empty-cancel)
- [Reservation TTL and the τ Property](#reservation-ttl-and-the-τ-property)
- [Persistence and Recovery](#persistence-and-recovery)
- [Project Structure](#project-structure)
- [Technologies](#technologies)
- [Requirements](#requirements)
- [Configuration](#configuration)
- [Running the Project](#running-the-project)
- [Authentication](#authentication)
- [API Endpoints](#api-endpoints)
- [Usage Examples](#usage-examples)
- [State Inspection](#state-inspection)
- [Testing with Postman](#testing-with-postman)
- [TTL Test](#ttl-test)
- [Manual Recovery Test](#manual-recovery-test)
- [Failure Scenarios](#failure-scenarios)
- [Limitations](#limitations)
- [Possible Improvements](#possible-improvements)

---

## Overview

This project simulates the creation of an order involving five independent microservices:

- an authentication service;
- an order coordinator;
- an inventory participant;
- a payment participant, integrated with the Stripe sandbox;
- a shipping participant.

The client authenticates through the `auth-service` and receives a JWT. The JWT is then sent to the `order-service`, which coordinates the distributed transaction across inventory, payment, and shipping.

Payments are not simulated with local accounts: the `payment-service` talks to the **Stripe sandbox** using Stripe's test payment methods (`pm_card_visa`, `pm_card_chargeDeclinedInsufficientFunds`, `pm_card_chargeDeclined`). No real money is ever moved. A static web page in `frontend/` lets you run the whole scenario from the browser.

The distributed transaction is implemented using the **Try-Confirm/Cancel pattern**.

The system is intentionally designed as a didactic project. Its primary objective is to demonstrate the semantics and failure handling of a distributed TCC transaction rather than provide a production-ready e-commerce platform.

---

## Goals

The project demonstrates the following distributed systems concepts:

- independent microservices;
- REST communication between services;
- JWT-based authentication;
- distributed transaction coordination;
- resource reservation;
- partial failure management;
- compensation through Cancel operations;
- idempotent participant operations;
- empty-cancel handling;
- time-bounded reservations through a configurable TTL;
- persistent coordinator decisions;
- retry mechanisms;
- recovery of incomplete transactions;
- separation between global decision and execution status;
- mapping of TCC onto a real external payment provider (authorize / capture / cancel);
- idempotency toward an external API through Stripe idempotency keys;
- containerized deployment with Docker Compose;
- a browser client that visualizes the TCC progress in real time.

---

## Architecture

```text
                               +------------------+
                               |      Client      |
                               +---------+--------+
                                         |
                                         | POST /login
                                         v
                               +------------------+
                               |   auth-service   |
                               |   JWT issuer     |
                               |      :8000       |
                               +---------+--------+
                                         |
                                         | JWT
                                         v
                               +------------------+
                               |  order-service   |
                               | TCC coordinator  |
                               |      :8001       |
                               +---+----------+---+
                                   |          |
                  +----------------+          +----------------+
                  |                                            |
                  v                                            v
        +-------------------+                         +-------------------+
        | inventory-service |                         |  payment-service  |
        | TCC participant   |                         | TCC participant   |
        |       :8002       |                         |       :8003       |
        +-------------------+                         +-------------------+
                                   |
                                   v
                         +-------------------+
                         | shipping-service  |
                         | TCC participant   |
                         |       :8004       |
                         +-------------------+
```

The `payment-service` also calls the Stripe API, and the web client in `frontend/` talks directly to every service (all of them enable CORS):

```text
   +----------------------+            +----------------------+
   |  Web client          |  HTTP/CORS |  auth / order /      |
   |  frontend/ (static)  +----------->|  inventory / payment |
   |                      |            |  / shipping          |
   +----------------------+            +-----------+----------+
                                                   |
                                       payment-service only
                                                   v
                                       +----------------------+
                                       |  Stripe sandbox      |
                                       |  (test mode, sk_test)|
                                       +----------------------+
```

Each service owns its own local state and communicates with the other services through HTTP APIs.

There is no shared in-memory state between services.

---

## Services

| Service | Host port | Responsibility |
|---|---:|---|
| `auth-service` | `8000` | Authenticates users and generates JWT access tokens |
| `order-service` | `8001` | Coordinates the distributed TCC transaction |
| `inventory-service` | `8002` | Reserves, confirms, or releases product quantities |
| `payment-service` | `8003` | Authorizes, captures, or releases card payments on the Stripe sandbox; registers the test card of each user |
| `shipping-service` | `8004` | Prepares, confirms, or cancels shipments |

Each service is implemented as an independent FastAPI application and runs inside its own Docker container.

---

## TCC Pattern

TCC divides a distributed business operation into three operations:

```text
Try
Confirm
Cancel
```

### Try

Each participant reserves the resource required by the transaction without applying the final business effect.

In this project:

- inventory increases the product's `reserved` quantity;
- payment creates a Stripe `PaymentIntent` with `capture_method=manual`, which **authorizes** the amount on the card without charging it;
- shipping creates a shipment in the `RESERVED` state.

A successful Try means:

> The participant guarantees that the resource is reserved and can later be either confirmed or cancelled.

Each reservation also receives an expiration timestamp:

```python
expires_at = time.time() + TTL_SECONDS
```

The reservation therefore remains valid only for a bounded interval `τ`, represented in the implementation by `TTL_SECONDS`.

### Confirm

If every Try succeeds, the coordinator decides `COMMIT` and asks every participant to make its reservation final.

In this project:

- inventory decreases both `reserved` and `stock`;
- payment **captures** the authorized `PaymentIntent`, so the money is actually charged;
- shipping changes the shipment state to `CONFIRMED`.

After the coordinator has persisted the `COMMIT` decision, a Confirm failure does **not** cause a rollback. The coordinator keeps the `COMMIT` decision and retries the missing Confirm operations.

### Cancel

If at least one Try fails before the commit decision, the coordinator decides `ROLLBACK` and asks the participants to release any reserved resources.

In this project:

- inventory decreases `reserved` without changing `stock`;
- payment **cancels** the `PaymentIntent`, releasing the authorization without any charge;
- shipping changes the shipment state to `CANCELLED`.

The coordinator sends Cancel to every participant. Participants that never created a reservation return a successful empty-cancel response.

---

## Stripe Sandbox Integration

The `payment-service` replaces the former simulated accounts (`balance` / `blocked`) with the Stripe **test mode** API. The service refuses to start unless `STRIPE_SECRET_KEY` begins with `sk_test_` or `rk_test_`: live keys (`sk_live_`), publishable keys (`pk_`), or an empty value stop the container with an explicit error message, so real money can never be charged.

### TCC to Stripe mapping

| TCC phase | Endpoint | Stripe operation | Result on the card |
|---|---|---|---|
| Try | `POST /tcc/try` | `PaymentIntent.create(capture_method="manual", confirm=True)` | Amount authorized, not charged (`requires_capture`) |
| Confirm | `PUT /tcc/confirm` | `PaymentIntent.capture(...)` | Amount charged (`succeeded`) |
| Cancel | `PUT /tcc/cancel` | `PaymentIntent.cancel(...)` | Authorization released (`canceled`) |
| TTL expiry | lazy check | `PaymentIntent.cancel(...)` | Authorization released |

Stripe is a natural fit for TCC: the authorize-then-capture model is exactly a Try followed by a Confirm, and cancelling an uncaptured `PaymentIntent` is the Cancel.

If the Try returns a `PaymentIntent` whose status is not `requires_capture`, the service cancels it and answers `409`.

### Test payment methods

Each user is associated with one Stripe test payment method:

| Payment method | Behavior |
|---|---|
| `pm_card_visa` | Authorization and capture succeed |
| `pm_card_chargeDeclinedInsufficientFunds` | Declined with *insufficient funds* |
| `pm_card_chargeDeclined` | Declined with a generic *card declined* |

The accepted format is `pm_card_<name>`, so any other Stripe test payment method can also be used. The default is `pm_card_visa` (`STRIPE_DEFAULT_PAYMENT_METHOD`).

### Users and Stripe Customers

```http
PUT  /users/{username}   body: { "payment_method": "pm_card_visa" }
GET  /users
```

`PUT /users/{username}` creates (or updates) a Stripe **Customer** with:

- email `<username>@tcc-demo.test`;
- metadata `username` and `test_payment_method`.

The Customer on Stripe is the source of truth: it is looked up by email, and an in-memory cache avoids repeated lookups. For this reason the card chosen for a user survives a restart of the container.

> Stripe's own REST API has no `PUT` or `PATCH`: updates are sent with `POST`. Even though this service exposes `PUT /users/{username}`, the Stripe SDK sends `POST /v1/customers/{id}` and that is what appears in the Stripe dashboard logs.

### Which card is charged

During Try, the payment method is chosen in this order:

1. `payment_method` sent in the `POST /orders` request;
2. the card registered for the user with `PUT /users/{username}`;
3. `STRIPE_DEFAULT_PAYMENT_METHOD` (`pm_card_visa`).

### Idempotency toward Stripe

The `Idempotency-Key` header is built from the transaction identifier:

```text
tcc-try-<transaction_id>
tcc-confirm-<transaction_id>
```

A retried Try or Confirm therefore never authorizes or captures the same amount twice. In addition:

- if a capture fails but the `PaymentIntent` is already `succeeded` (lost response), Confirm answers `ALREADY_CONFIRMED`;
- the SDK is configured with `max_network_retries = 2`.

### Error translation

| Stripe error | HTTP status returned | Effect on the coordinator |
|---|---|---|
| `CardError` (declined card, insufficient funds) | `409` | Try fails, `ROLLBACK` |
| `InvalidRequestError` | `409` | Try fails, `ROLLBACK` |
| `APIConnectionError`, `RateLimitError` | `503` | Try fails; a Confirm/Cancel is retried |
| any other Stripe error | `502` | Try fails; a Confirm/Cancel is retried |

### Amounts

Amounts are converted from euros to cents with `int(round(amount * 100))`, because Stripe works in minor units. The currency comes from `STRIPE_CURRENCY`.

### Link with the order log

The coordinator stores the identifier of the authorization in the transaction log:

```json
"payment_intent_id": "pi_..."
```

The same value is returned by `POST /orders`, so each order can be matched with the corresponding payment in the Stripe dashboard (the `PaymentIntent` also carries `transaction_id` and `username` in its metadata).

---

## Web Client

The `frontend/` directory contains a small static client written in plain HTML, CSS, and JavaScript. It has no build step and no dependencies.

```text
frontend/
├── index.html
├── script.js
└── style.css
```

### Running the client

Start the backend first, then either open `frontend/index.html` directly in the browser or serve the folder:

```bash
cd frontend
python -m http.server 5500
```

and open `http://localhost:5500`. The client calls the services on ports `8000`-`8004` of the same host (`localhost` when opened from a file). Every service enables CORS, so no proxy is needed.

### What the page does

| Step | Action | Backend call |
|---|---|---|
| Header | Shows an up/down indicator for each service | `GET /health` on all five services |
| 1. Login | Authenticates and keeps the JWT in memory | `POST /login` on `auth-service` |
| 2. Test card | Associates one of the three Stripe test cards with the logged-in user | `PUT /users/{username}` on `payment-service` |
| 3. Order | Sends product, quantity, address, `fail_payment`, `fail_shipping`, and the selected card | `POST /orders` on `order-service` |
| TCC progress | Table with Try / Confirm / Cancel of each participant, plus `status`, `decision`, and `PaymentIntent` | `GET /orders`, polled every 400 ms during the order |
| Service state | Raw `/state` JSON of inventory, payment, and shipping | `GET /state` |

The order button is enabled only after login and card association, and the quantity is validated before sending. While the request is running, the page polls the coordinator, so the Try, Confirm, and Cancel columns can be seen changing in real time. At the end the raw HTTP response is shown.

### Demo scenarios

| Card | Flags | Expected outcome |
|---|---|---|
| `pm_card_visa` | none | `COMMIT` / `CONFIRMED`: authorization captured on Stripe |
| `pm_card_chargeDeclinedInsufficientFunds` | none | Payment Try fails, `ROLLBACK` / `CANCELLED` (HTTP `409`) |
| `pm_card_chargeDeclined` | none | Payment Try fails, `ROLLBACK` / `CANCELLED` (HTTP `409`) |
| `pm_card_visa` | `fail_shipping` | Payment authorized, shipping Try fails, authorization **cancelled** on Stripe |
| any | `fail_payment` | Simulated payment error, `ROLLBACK` |

### Scope of the client

The client is a demonstration tool and intentionally keeps things simple:

- it **does not implement recovery**: there is no button for `POST /orders/{transaction_id}/recover`, so a transaction left in `CONFIRM_PENDING` or `CANCEL_PENDING` has to be recovered with Postman, curl, or by restarting the order service;
- the progress panel shows only the order just created, not the history of previous orders;
- the JWT is kept in a JavaScript variable only (not in `localStorage`), so reloading the page requires a new login;
- the order does not send a card: the backend uses the one registered in step 2, so changing the card in the dropdown disables the order button until the card is associated again;
- errors show the failed participant and the reason, and an expired JWT (HTTP `401`) asks for a new login.

---

## Transaction Decision and Status

The order coordinator stores two separate fields:

```text
decision
status
```

They represent different concepts.

### Decision

`decision` answers:

> What is the final global outcome selected by the coordinator?

Possible values:

| Decision | Meaning |
|---|---|
| `UNDECIDED` | The Try phase is still in progress |
| `COMMIT` | Every Try succeeded; the transaction must be confirmed |
| `ROLLBACK` | At least one Try failed; the transaction must be cancelled |

Once the coordinator persists `COMMIT`, it never changes the decision to `ROLLBACK`.

### Status

`status` answers:

> At which execution stage is the transaction currently located?

Possible values:

| Status | Meaning |
|---|---|
| `TRYING` | The coordinator is executing participant Try operations |
| `CONFIRMING` | The coordinator has decided `COMMIT` and is executing Confirm |
| `CONFIRM_PENDING` | At least one Confirm has not yet completed |
| `CONFIRMED` | Every participant has completed Confirm |
| `CANCELLING` | The coordinator has decided `ROLLBACK` and is executing Cancel |
| `CANCEL_PENDING` | At least one Cancel has not yet completed |
| `CANCELLED` | Every participant has completed Cancel |

Examples:

```text
decision = COMMIT
status   = CONFIRMED
```

The transaction completed successfully.

```text
decision = COMMIT
status   = CONFIRM_PENDING
```

The coordinator has already committed, but at least one participant still needs to be confirmed.

```text
decision = ROLLBACK
status   = CANCEL_PENDING
```

The transaction must be cancelled, but at least one participant still needs to process Cancel.

### Last error

The coordinator also stores:

```text
last_error
```

This field is diagnostic and records the most recent failure, such as:

- participant name;
- failed operation;
- retry attempt;
- error message.

Example:

```json
{
  "participant": "payment",
  "operation": "confirm",
  "attempt": 5,
  "message": "Connection refused"
}
```

`last_error` does not determine the TCC decision. It helps explain why a transaction remains pending.

---

## Distributed Order Flow

### Successful transaction

```text
1. Create transaction
   decision = UNDECIDED
   status   = TRYING

2. TRY inventory
3. TRY payment
4. TRY shipping

5. Persist:
   decision = COMMIT
   status   = CONFIRMING

6. CONFIRM inventory
7. CONFIRM payment
8. CONFIRM shipping

9. Persist:
   decision = COMMIT
   status   = CONFIRMED
```

### Failure during Try

```text
1. TRY inventory -> OK
2. TRY payment   -> FAIL

3. Persist:
   decision = ROLLBACK
   status   = CANCELLING

4. CANCEL shipping  -> empty cancel
5. CANCEL payment   -> empty cancel or cancel
6. CANCEL inventory -> release reservation

7. Persist:
   decision = ROLLBACK
   status   = CANCELLED
```

### Failure during Confirm

```text
1. Every TRY succeeds

2. Persist:
   decision = COMMIT
   status   = CONFIRMING

3. CONFIRM inventory -> OK
4. CONFIRM payment   -> FAIL

5. Retry CONFIRM payment

6. If retries are temporarily exhausted:
   decision = COMMIT
   status   = CONFIRM_PENDING
```

The coordinator does not call Cancel after the `COMMIT` decision.

The pending transaction can later be recovered manually or when the order service restarts.

---

## Idempotency and Empty Cancel

Network communication can fail after a participant has processed a request but before the coordinator receives the response.

For this reason, the participant operations are idempotent.

### Idempotent Try

The same `transaction_id` and the same payload do not reserve the resource twice.

Example:

```text
First Try  -> RESERVED
Second Try -> ALREADY_PROCESSED
```

If the same `transaction_id` is reused with a different payload, the participant returns:

```text
409 Conflict
```

### Idempotent Confirm

A repeated Confirm does not apply the final effect twice.

Example:

```text
First Confirm  -> CONFIRMED
Second Confirm -> ALREADY_CONFIRMED
```

### Idempotent Cancel

A repeated Cancel does not release the same resource twice.

Example:

```text
First Cancel  -> CANCELLED
Second Cancel -> ALREADY_CANCELLED
```

### Empty Cancel

The coordinator may not know whether a Try reached a participant.

A Cancel for a transaction that does not exist returns success:

```json
{
  "status": "NOTHING_TO_CANCEL",
  "transaction_id": "example-id",
  "state": "IDLE",
  "empty_cancel": true
}
```

This allows the coordinator to safely send Cancel to every participant during rollback.

---


## Reservation TTL and the τ Property

A successful TCC Try creates a provisional reservation. Without an expiration policy, a coordinator crash or a permanently lost second-phase request could leave inventory, funds, or shipping capacity reserved indefinitely.

To prevent abandoned reservations from remaining blocked forever, every participant implements a configurable **Time To Live (TTL)**. The TTL represents the temporal bound commonly denoted by `τ`.

### Reservation creation

When a participant successfully processes Try, it stores:

```python
"expires_at": time.time() + TTL_SECONDS
```

The current Docker Compose configuration passes the same value to the three participants:

```yaml
inventory-service:
  environment:
    - TTL_SECONDS=60

payment-service:
  environment:
    - TTL_SECONDS=60

shipping-service:
  environment:
    - TTL_SECONDS=60
```

The value `60` seconds is suitable for normal execution. For the TTL tests described below it can be temporarily lowered to `10`.

### Lazy expiration strategy

The project implements expiration with an **inline lazy check**, rather than a background scheduler.

Each participant calls its expiration function at the beginning of:

```text
POST /tcc/try
PUT  /tcc/confirm
PUT  /tcc/cancel
```

The check is performed only for the `transaction_id` received by the current request.

This means that a reservation is released when a later TCC request for the same transaction reaches the participant. The project does not run a periodic background task that scans all reservations.

### Inventory expiration

If an inventory reservation is still `RESERVED` after `expires_at`:

```text
reserved decreases
stock remains unchanged
state becomes CANCELLED
expired becomes true
```

The product quantity is released because it was never committed.

### Payment expiration

If a payment reservation is still `RESERVED` after `expires_at`:

```text
the Stripe PaymentIntent is cancelled
state becomes CANCELLED
expired becomes true
```

The authorization on the card is released without charging anything.

### Shipping expiration

If a shipment is still `RESERVED` after `expires_at`:

```text
state becomes CANCELLED
expired becomes true
```

The shipment can no longer be confirmed.

### Confirm after expiration

When Confirm reaches an expired reservation, the participant first applies the expiration rule and then rejects Confirm with:

```text
409 Conflict
```

The local record is retained with:

```json
{
  "state": "CANCELLED",
  "expired": true
}
```

The record is not deleted. Keeping the tombstone-like state preserves idempotency and prevents the same transaction identifier from creating a new reservation after expiration.

### TTL and a persisted COMMIT decision

The TTL must be configured carefully.

The coordinator persists `COMMIT` before sending Confirm. After that decision, the transaction must converge toward Confirm and must not be rolled back.

A TTL that is too short may create this scenario:

```text
1. Every Try succeeds.
2. The coordinator persists COMMIT.
3. A participant becomes temporarily unreachable.
4. The participant reservation reaches its TTL.
5. Confirm is retried after the participant becomes reachable.
6. The participant expires the reservation and rejects Confirm.
```

For this reason, the configured TTL should be longer than the maximum expected time required by the coordinator to execute retries and recovery.

With the current order-service configuration:

```text
HTTP timeout          = 5 seconds
MAX_RETRY_ATTEMPTS    = 5
RETRY_DELAY_SECONDS   = 1 second
```

a participant failure can keep one second-phase operation active for approximately 29 seconds in the worst case. Therefore:

```text
TTL_SECONDS=60
```

is longer than this window and is the value used by the current configuration, while:

```text
TTL_SECONDS=10
```

should be treated only as a test value for the TTL tests.

A production implementation would normally add persistent participant state, renewable leases, and a reconciliation mechanism between the coordinator decision and expired reservations.

### Anti-hanging scope

The TTL prevents an existing `RESERVED` entry from remaining blocked indefinitely and prevents an expired transaction identifier from being reused.

The current empty-cancel implementation does not create a cancellation tombstone when Cancel arrives for a completely unknown transaction. Therefore, a severely delayed Try that arrives after an earlier empty-cancel is not explicitly rejected.

This is a known simplification of the didactic implementation. A complete anti-hanging solution would persist a cancelled marker even for an empty-cancel transaction identifier.

---

## Persistence and Recovery

The coordinator stores its transaction log in:

```text
/data/orders.json
```

The directory is mounted through the Docker volume:

```text
order-data
```

The log contains:

- request data;
- transaction ID;
- username, product, quantity, amount, and address;
- Stripe `payment_intent_id`;
- decision;
- status;
- participant Try/Confirm/Cancel states;
- last error.

### Why persistence is required

Consider the following sequence:

```text
1. Every Try succeeds
2. Coordinator persists COMMIT
3. Inventory Confirm succeeds
4. Order service crashes
```

After restarting, the coordinator must remember that the decision was `COMMIT`.

It must continue Confirm. It must not start Cancel.

### Automatic recovery

When `order-service` starts, it loads `orders.json` and searches for transactions in one of these states:

```text
TRYING
CONFIRMING
CONFIRM_PENDING
CANCELLING
CANCEL_PENDING
```

Recovery follows the persisted decision:

| Persisted decision | Recovery action |
|---|---|
| `COMMIT` | Retry Confirm |
| `ROLLBACK` | Retry Cancel |
| `UNDECIDED` | Choose rollback and execute Cancel |

### Manual recovery

A transaction can also be recovered through:

```http
POST /orders/{transaction_id}/recover
```

The endpoint retries the correct second-phase operation according to the persisted decision.

---

## Project Structure

```text
Distributed-System-with-JWT-TCC/
│
├── services/
│   ├── auth-service/
│   │   ├── main.py
│   │   ├── requirements.txt
│   │   └── Dockerfile
│   ├── order-service/
│   │   ├── main.py
│   │   ├── requirements.txt
│   │   └── Dockerfile
│   ├── inventory-service/
│   │   ├── main.py
│   │   ├── requirements.txt
│   │   └── Dockerfile
│   ├── payment-service/          (Stripe sandbox integration)
│   │   ├── main.py
│   │   ├── requirements.txt
│   │   └── Dockerfile
│   └── shipping-service/
│       ├── main.py
│       ├── requirements.txt
│       └── Dockerfile
│
├── frontend/
│   ├── index.html
│   ├── script.js
│   └── style.css
│
├── postman/
│   ├── Distributed_TCC_Complete_Test_Suite.postman_collection.json
│   ├── Stripe_Sandbox.postman_collection.json
│   └── Distributed_TCC_Local.postman_environment.json
│
├── docker-compose.yml
├── .env                          (not versioned: Stripe key)
├── .gitignore
└── README.md
```

---

## Technologies

- Python 3.12
- FastAPI
- Uvicorn
- HTTPX
- python-jose
- JWT
- Stripe Python SDK (sandbox / test mode)
- Docker
- Docker Compose
- Postman
- HTML, CSS, and vanilla JavaScript (web client)

---

## Requirements

To run the project, install:

- Docker;
- Docker Compose;
- a free Stripe account with **test mode** enabled and a secret test key (`sk_test_...`), available in the Stripe dashboard under *Developers → API keys*;
- Postman, optionally, for API testing;
- a web browser for the web client.

No local Python installation is required when running the system through Docker.

---

## Configuration

The main Docker Compose environment variables are:

| Variable | Service | Default purpose |
|---|---|---|
| `JWT_SECRET` | auth, order | Shared HS256 development secret |
| `INVENTORY_SERVICE_URL` | order | Internal inventory URL |
| `PAYMENT_SERVICE_URL` | order | Internal payment URL |
| `SHIPPING_SERVICE_URL` | order | Internal shipping URL |
| `MAX_RETRY_ATTEMPTS` | order | Number of Confirm/Cancel attempts |
| `RETRY_DELAY_SECONDS` | order | Delay between attempts |
| `ORDERS_FILE` | order | Persistent transaction log location |
| `TTL_SECONDS` | inventory, payment, shipping | Maximum lifetime of a `RESERVED` participant reservation |
| `STRIPE_SECRET_KEY` | payment | Stripe **test** secret key (`sk_test_...` or `rk_test_...`); required |
| `STRIPE_CURRENCY` | payment | Currency of the PaymentIntents, for example `eur` |
| `STRIPE_DEFAULT_PAYMENT_METHOD` | payment | Card used when none is specified; defaults to `pm_card_visa` (not set in the Compose file) |

The Compose configuration provides development defaults.

`TTL_SECONDS` is currently set to `60`, which is greater than the coordinator retry window. Use a lower value (for example `10`) only for the TTL tests.

### Stripe configuration (`.env`)

The Stripe variables are read by Docker Compose from a `.env` file in the project root. The file is listed in `.gitignore` and must never be committed. Create it before starting the system:

```env
STRIPE_SECRET_KEY=sk_test_your_key_here
STRIPE_CURRENCY=eur
```

`STRIPE_SECRET_KEY` is required. `STRIPE_CURRENCY` is optional: `docker-compose.yml` forwards it as `${STRIPE_CURRENCY:-eur}`, so `eur` is used when it is not defined.

If the key is missing or is not a test key, the `payment-service` container exits at startup with a message explaining the reason (the key itself is never printed).

For a real deployment, secrets must not be stored in the repository or committed to version control.

---

## Running the Project

### Clean start

From the project root:

```bash
docker compose down -v
docker compose up --build -d
```

`-v` removes the persistent order volume and starts from an empty transaction log.

### Normal restart

To restart without deleting the coordinator log:

```bash
docker compose down
docker compose up --build -d
```

Do not use `-v` when testing persistence.

### Check containers

```bash
docker compose ps
```

### View logs

```bash
docker compose logs -f
```

Only the coordinator:

```bash
docker compose logs -f order-service
```

### Service URLs

| Service | URL |
|---|---|
| Auth | `http://localhost:8000` |
| Order | `http://localhost:8001` |
| Inventory | `http://localhost:8002` |
| Payment | `http://localhost:8003` |
| Shipping | `http://localhost:8004` |

### Web client

Open `frontend/index.html` in the browser (see [Web Client](#web-client)).

### Swagger documentation

| Service | Swagger |
|---|---|
| Auth | `http://localhost:8000/docs` |
| Order | `http://localhost:8001/docs` |
| Inventory | `http://localhost:8002/docs` |
| Payment | `http://localhost:8003/docs` |
| Shipping | `http://localhost:8004/docs` |

---

## Authentication

The system uses JWT authentication for order creation.

### Demo users

| Username | Password | Role |
|---|---|---|
| `gabriele` | `gabrielepass` | customer |
| `carlos` | `admin` | admin |

These credentials are hard-coded for demonstration purposes.

### Login

```http
POST http://localhost:8000/login
Content-Type: application/json
```

Request:

```json
{
  "username": "carlos",
  "password": "admin"
}
```

Response:

```json
{
  "access_token": "eyJhbGciOiJIUzI1NiIs...",
  "token_type": "bearer"
}
```

### Use the token

```http
Authorization: Bearer YOUR_ACCESS_TOKEN
```

The token is valid for 60 minutes. The `order-service` verifies the token signature, expiration, and the presence of the `sub` claim before starting the distributed transaction. The `sub` claim (the username) is also the user whose Stripe card is charged.

---

## API Endpoints

### Auth service

```http
GET  /health
POST /login
```

### Order service

```http
GET  /health
GET  /orders
GET  /orders/{transaction_id}
POST /orders
POST /orders/{transaction_id}/recover
```

`POST /orders` accepts the optional field `payment_method` (for example `pm_card_visa`), which selects the Stripe test card for that order.

### Inventory service

```http
GET  /health
GET  /products
GET  /products/{product_id}
GET  /state
POST /tcc/try
PUT  /tcc/confirm
PUT  /tcc/cancel
```

### Payment service

```http
GET  /health
GET  /state
GET  /users
PUT  /users/{username}
POST /tcc/try
PUT  /tcc/confirm
PUT  /tcc/cancel
```

`PUT /users/{username}` registers the Stripe test card of a user. The username must match `^[a-z0-9_.-]{1,50}$`.

### Shipping service

```http
GET  /health
GET  /state
POST /tcc/try
PUT  /tcc/confirm
PUT  /tcc/cancel
```

---

## Usage Examples

### Health check

```bash
curl http://localhost:8000/health
curl http://localhost:8001/health
curl http://localhost:8002/health
curl http://localhost:8003/health
curl http://localhost:8004/health
```

### Login with curl

```bash
TOKEN=$(
  curl -s \
    -X POST http://localhost:8000/login \
    -H "Content-Type: application/json" \
    -d '{
      "username": "carlos",
      "password": "admin"
    }' |
  jq -r '.access_token'
)
```

### Successful order

```http
POST http://localhost:8001/orders
Authorization: Bearer YOUR_ACCESS_TOKEN
Content-Type: application/json
```

```json
{
  "product_id": "p2",
  "quantity": 2,
  "address": "Via Roma 1",
  "fail_payment": false,
  "fail_shipping": false
}
```

Example response:

```json
{
  "status": "ORDER_CONFIRMED",
  "transaction_id": "a-generated-uuid",
  "product_id": "p2",
  "quantity": 2,
  "unit_price": 69.99,
  "amount": 139.98,
  "payment_intent_id": "pi_...",
  "decision": "COMMIT",
  "participants": {
    "inventory": {
      "try": "OK",
      "confirm": "OK",
      "cancel": "NOT_STARTED"
    },
    "payment": {
      "try": "OK",
      "confirm": "OK",
      "cancel": "NOT_STARTED"
    },
    "shipping": {
      "try": "OK",
      "confirm": "OK",
      "cancel": "NOT_STARTED"
    }
  }
}
```

### Register a Stripe test card for a user

```http
PUT http://localhost:8003/users/carlos
Content-Type: application/json
```

```json
{
  "payment_method": "pm_card_chargeDeclinedInsufficientFunds"
}
```

Response:

```json
{
  "username": "carlos",
  "customer_id": "cus_...",
  "payment_method": "pm_card_chargeDeclinedInsufficientFunds"
}
```

### Order paid with Stripe

```json
{
  "product_id": "p1",
  "quantity": 1,
  "address": "Via Roma 1",
  "fail_payment": false,
  "fail_shipping": false,
  "payment_method": "pm_card_visa"
}
```

Expected flow:

```text
TRY payment     -> PaymentIntent authorized (requires_capture)
CONFIRM payment -> PaymentIntent captured   (succeeded)
```

### Declined card

Using `pm_card_chargeDeclinedInsufficientFunds` or `pm_card_chargeDeclined`:

```text
TRY inventory -> OK
TRY payment   -> FAIL (Stripe card error, HTTP 409)

decision = ROLLBACK
status   = CANCELLED
```

The inventory reservation is released and no payment remains authorized.

### Simulated payment failure

```json
{
  "product_id": "p2",
  "quantity": 1,
  "address": "Via Roma 1",
  "fail_payment": true,
  "fail_shipping": false
}
```

Expected flow:

```text
TRY inventory -> OK
TRY payment   -> FAIL

decision = ROLLBACK
status   = CANCELLING

CANCEL shipping  -> empty cancel
CANCEL payment   -> empty cancel
CANCEL inventory -> OK

status = CANCELLED
```

### Simulated shipping failure

```json
{
  "product_id": "p2",
  "quantity": 1,
  "address": "Via Roma 1",
  "fail_payment": false,
  "fail_shipping": true
}
```

Expected flow:

```text
TRY inventory -> OK
TRY payment   -> OK
TRY shipping  -> FAIL

decision = ROLLBACK
status   = CANCELLING

CANCEL shipping  -> empty cancel
CANCEL payment   -> OK
CANCEL inventory -> OK

status = CANCELLED
```

---

## State Inspection

The internal state can be inspected through:

```http
GET http://localhost:8001/orders
GET http://localhost:8002/state
GET http://localhost:8003/state
GET http://localhost:8004/state
```

### Inventory state

A product contains:

```text
stock
reserved
```

During Try:

```text
stock unchanged
reserved increased
```

After Confirm:

```text
stock decreased
reserved decreased
```

After Cancel:

```text
stock unchanged
reserved decreased
```

After TTL expiration:

```text
stock unchanged
reserved decreased
state = CANCELLED
expired = true
```

### Payment state

`GET /state` on the payment service returns, for each transaction:

```text
transaction_id
username
amount
payment_intent_id
payment_method
state
expires_at
expired
```

The money itself is kept by Stripe, not by the service. The local `state` follows the status of the `PaymentIntent`:

| Local state | Stripe PaymentIntent | Meaning |
|---|---|---|
| `RESERVED` | `requires_capture` | Amount authorized on the card |
| `CONFIRMED` | `succeeded` | Amount captured |
| `CANCELLED` | `canceled` | Authorization released |

After TTL expiration:

```text
state = CANCELLED
expired = true
PaymentIntent cancelled on Stripe
```

The same information can be checked in the Stripe dashboard (test mode) under *Payments*.

### Shipping state

A shipment moves between:

```text
RESERVED
CONFIRMED
CANCELLED
```

If the TTL expires while the shipment is `RESERVED`, the state becomes `CANCELLED` and the record receives `expired = true`.

---

## Testing with Postman

The `postman/` directory contains:

```text
Distributed_TCC_Complete_Test_Suite.postman_collection.json
Stripe_Sandbox.postman_collection.json
Distributed_TCC_Local.postman_environment.json
```

### Import

1. Start the services.
2. Open Postman.
3. Import the collection.
4. Import the environment.
5. Select `Distributed TCC Local`.
6. Run `Login corretto - salva token`.
7. Run the remaining requests.

The login script stores the JWT both as an environment variable and as a collection variable:

```javascript
pm.environment.set("token", body.access_token);
pm.collectionVariables.set("token", body.access_token);
```

Requests requiring authentication use:

```http
Authorization: Bearer {{token}}
```

### Recommended execution order

```text
00 - Health
01 - Auth
02 - Orders
03 - Inventory direct
04 - Payment direct
05 - Shipping direct
06 - Recovery manuale
```

### Stripe collection

`Stripe_Sandbox.postman_collection.json` contains the payment scenarios and is organized in three folders:

| Folder | Content |
|---|---|
| `01 - Flusso ordine con Stripe` | Payment health, login, successful order (authorize + capture), order with a declined card (`ROLLBACK`), order with a shipping error (`ROLLBACK`, Stripe cancel), payment state checks |
| `03 - Utenti con carta Stripe` | Registers `carlos` with a valid card and `gabriele` with insufficient funds, then runs one order for each user, and lists the registered users |
| `02 - Payment diretto` | Direct calls to the payment participant: Try, idempotent Try, Confirm (capture), repeated Confirm, Cancel after Confirm (`409`), declined card, Try + Cancel |

The folders are stored in the file in the order `01`, `03`, `02`. Run them after the stack has started with a valid `STRIPE_SECRET_KEY`, and check the resulting `PaymentIntent` objects in the Stripe dashboard.

The original suite also contains the direct participant tests, which use fixed transaction IDs. Before repeating the entire suite, either:

- change the variables `inventoryTx`, `paymentTx`, and `shippingTx`;
- or restart the participant containers to reset their in-memory state.

---


## TTL Test

The Compose value is `TTL_SECONDS=60`. To test expiration quickly, temporarily set `TTL_SECONDS=10` for the three participants in `docker-compose.yml` and recreate them:

```bash
docker compose up --build -d \
  inventory-service \
  payment-service \
  shipping-service
```

The steps below assume a TTL of 10 seconds.

### Inventory TTL

Create a reservation:

```http
POST http://localhost:8002/tcc/try
Content-Type: application/json
```

```json
{
  "transaction_id": "ttl-inventory-1",
  "product_id": "p1",
  "quantity": 2
}
```

Immediately inspect:

```http
GET http://localhost:8002/state
```

Expected state:

```text
state = RESERVED
reserved = 2
expires_at = a future Unix timestamp
expired = false
```

Wait more than 10 seconds, then send:

```http
PUT http://localhost:8002/tcc/confirm
Content-Type: application/json
```

```json
{
  "transaction_id": "ttl-inventory-1"
}
```

Expected result:

```text
409 Conflict
```

The state must now show:

```text
stock unchanged
reserved = 0
state = CANCELLED
expired = true
```

### Payment TTL

Create a reservation:

```http
POST http://localhost:8003/tcc/try
Content-Type: application/json
```

```json
{
  "transaction_id": "ttl-payment-1",
  "username": "carlos",
  "amount": 100,
  "fail": false,
  "payment_method": "pm_card_visa"
}
```

Before expiration:

```text
state = RESERVED
PaymentIntent in the Stripe dashboard: requires_capture (authorized, not charged)
```

After waiting more than the TTL, send Confirm for the same transaction.

Expected result:

```text
409 Conflict
state = CANCELLED
expired = true
PaymentIntent in the Stripe dashboard: canceled
```

### Shipping TTL

Create a reservation:

```http
POST http://localhost:8004/tcc/try
Content-Type: application/json
```

```json
{
  "transaction_id": "ttl-shipping-1",
  "username": "carlos",
  "address": "TTL Test Street 1",
  "fail": false
}
```

After waiting more than the TTL, send Confirm:

```http
PUT http://localhost:8004/tcc/confirm
Content-Type: application/json
```

```json
{
  "transaction_id": "ttl-shipping-1"
}
```

Expected result:

```text
409 Conflict
state = CANCELLED
expired = true
```

### Return to a normal TTL

After the expiration tests, restore the Compose configuration to:

```yaml
environment:
  - TTL_SECONDS=60
```

Then recreate the participant containers:

```bash
docker compose up --build -d \
  inventory-service \
  payment-service \
  shipping-service
```

---

## Manual Recovery Test

The following test creates a rollback that cannot initially complete.

### 1. Start from a clean environment

```bash
docker compose down -v
docker compose up --build -d
```

### 2. Stop shipping

```bash
docker compose stop shipping-service
```

### 3. Create an order

Send a normal order request through Postman.

Expected behavior:

```text
TRY inventory -> OK
TRY payment   -> OK
TRY shipping  -> connection failure

decision = ROLLBACK
```

Because shipping is unavailable, its Cancel also fails. The payment Cancel, however, is executed during the rollback, so the Stripe authorization is released even if the transaction stays `CANCEL_PENDING`.

The transaction can remain:

```text
decision = ROLLBACK
status   = CANCEL_PENDING
```

### 4. Find the transaction ID

```http
GET http://localhost:8001/orders
```

### 5. Restart shipping

```bash
docker compose start shipping-service
```

### 6. Recover the transaction

```http
POST http://localhost:8001/orders/TRANSACTION_ID/recover
```

Expected result:

```json
{
  "transaction_id": "TRANSACTION_ID",
  "completed": true,
  "status": "CANCELLED",
  "decision": "ROLLBACK"
}
```

### Automatic recovery

Instead of calling the recovery endpoint, restart only the coordinator:

```bash
docker compose restart order-service
```

At startup, the coordinator loads the persistent log and retries the incomplete transaction.

---

## Failure Scenarios

| Scenario | Decision | Final or pending status |
|---|---|---|
| Every Try and Confirm succeeds | `COMMIT` | `CONFIRMED` |
| Inventory Try fails | `ROLLBACK` | `CANCELLED` or `CANCEL_PENDING` |
| Payment Try fails | `ROLLBACK` | `CANCELLED` or `CANCEL_PENDING` |
| Stripe declines the card (`pm_card_chargeDeclined...`) | `ROLLBACK` | `CANCELLED` or `CANCEL_PENDING` |
| Shipping Try fails | `ROLLBACK` | `CANCELLED` or `CANCEL_PENDING` |
| Confirm temporarily fails after commit | `COMMIT` | `CONFIRM_PENDING` |
| Stripe temporarily unreachable during Confirm | `COMMIT` | `CONFIRM_PENDING` (retried) |
| Cancel temporarily fails after rollback | `ROLLBACK` | `CANCEL_PENDING` |
| Coordinator restarts during Try | `ROLLBACK` during recovery | `CANCELLED` or `CANCEL_PENDING` |
| Coordinator restarts after commit | `COMMIT` remains unchanged | `CONFIRMED` or `CONFIRM_PENDING` |
| Participant reservation expires before Confirm | Coordinator decision may still be `COMMIT` | Participant returns `409`; manual reconciliation is required |
| `payment-service` restarts after a successful Try | `COMMIT` | `CONFIRM_PENDING`: Confirm returns `404`; the authorization stays on Stripe |

---

## Limitations

This is a didactic implementation.

The following limitations are intentional:

- inventory and shipping keep their state in memory, and the payment service keeps in memory the index that links each transaction to its Stripe `PaymentIntent`;
- participant state is lost when the corresponding container restarts;
- only the order coordinator transaction log is persisted;
- concurrent updates are not synchronized with locks or database transactions;
- monetary values use floating-point numbers;
- internal participant endpoints are exposed on host ports for testing;
- participant endpoints do not use service-to-service authentication;
- passwords are hard-coded and stored in plain text;
- the development JWT secret has a Compose default;
- retry uses a fixed delay rather than exponential backoff;
- participant TTL expiration is lazy and runs only when another TCC request for the same transaction arrives;
- `TTL_SECONDS=10`, used for the TTL tests, is shorter than the worst-case coordinator retry window; the default `60` is longer;
- a reservation may expire after a persisted `COMMIT`, requiring reconciliation;
- empty-cancel does not persist a tombstone for completely unknown transaction identifiers, so full anti-hanging protection is not implemented;
- the application does not include distributed tracing or metrics;
- `GET /orders` and diagnostic state endpoints are not access-controlled;
- the services allow CORS from any origin (`*`) so that the web client can call them directly;
- Stripe is used only in sandbox mode and no webhooks are implemented: the service relies on synchronous API responses;
- the web client does not implement recovery and shows only the order just created.

### Recovery scope

Recovery is reliable for coordinator restarts while participant containers retain their in-memory state.

If a participant restarts after creating a reservation, it loses that reservation and may return `404` to a later Confirm.

If the `payment-service` restarts after a successful Try, the link between the transaction and its `PaymentIntent` is lost: Confirm returns `404` and the authorization stays open on Stripe until it is cancelled manually from the dashboard or expires on Stripe's side. A production implementation would persist participant state in databases (or look the `PaymentIntent` up through its `transaction_id` metadata).

---

## Possible Improvements

Future developments could focus on the following areas:

### Reliability and Persistence

* Persistent databases for every participant.
* Rebuild the payment state from Stripe (search by `transaction_id` metadata) after a restart.
* Stripe webhooks to reconcile asynchronous payment events.
* Transactional local state updates.
* A background recovery worker for pending transactions.
* Background expiration scanning for TTL reservations.
* Complete anti-hanging protection through cancellation tombstones.

### Concurrency and Data Consistency

* Optimistic or pessimistic concurrency control.
* Monetary amounts represented with `Decimal` or integer cents.

### Security

* Asymmetric JWT signing.
* Service-to-service authentication.
* Protected diagnostic and state inspection endpoints.

### Web Client

* A recovery button for `POST /orders/{transaction_id}/recover`.
* History of all orders and live display of the Stripe `PaymentIntent` status.

### Observability and Testing

* Structured logging and correlation IDs.
* Automated unit and integration tests.
* Docker health checks and readiness checks.

### Build and Delivery

* Dependency version locking.
* A continuous integration and continuous delivery pipeline.



---

## Key Takeaways

* The Try phase reserves resources without applying the final business effect.
* If every Try succeeds, the coordinator persists the `COMMIT` decision and executes Confirm on all participants.
* If a Try fails, the coordinator persists `ROLLBACK` and executes Cancel to release temporary reservations.
* After `COMMIT`, a failed Confirm must be retried and must not be converted into a rollback.
* Try, Confirm, and Cancel are idempotent, allowing the coordinator to safely repeat requests after network failures.
* Empty-cancel allows participants to accept Cancel even when no local reservation exists.
* The coordinator persists transaction state and can recover incomplete Confirm or Cancel operations after a restart.
* Participant reservations have a configurable TTL, which prevents provisional resources from remaining reserved indefinitely once expiration is detected.
* The payment participant maps TCC onto Stripe: Try authorizes the card, Confirm captures the payment, Cancel releases the authorization.
* `decision` represents the final outcome selected by the coordinator, while `status` represents the current progress toward that outcome.
* The project is intentionally simplified for educational purposes: participant state is in memory, payments run only on the Stripe sandbox, TTL expiration is lazy, and full anti-hanging protection is outside the current scope.