# Distributed System with JWT and TCC

A microservices-based distributed order management system built with **FastAPI**, **JWT authentication**, **Docker Compose**, and the **Try-Confirm/Cancel (TCC)** transaction pattern.

The project demonstrates how a coordinator can manage a distributed business operation across independent services while handling partial failures, compensation, idempotent operations, persistent decisions, retries, and transaction recovery.

---

## Table of Contents

- [Overview](#overview)
- [Goals](#goals)
- [Architecture](#architecture)
- [Services](#services)
- [TCC Pattern](#tcc-pattern)
- [Transaction Decision and Status](#transaction-decision-and-status)
- [Distributed Order Flow](#distributed-order-flow)
- [Idempotency and Empty Cancel](#idempotency-and-empty-cancel)
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
- a payment participant;
- a shipping participant.

The client authenticates through the `auth-service` and receives a JWT. The JWT is then sent to the `order-service`, which coordinates the distributed transaction across inventory, payment, and shipping.

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
- persistent coordinator decisions;
- retry mechanisms;
- recovery of incomplete transactions;
- separation between global decision and execution status;
- containerized deployment with Docker Compose.

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

Each service owns its own local state and communicates with the other services through HTTP APIs.

There is no shared in-memory state between services.

---

## Services

| Service | Host port | Responsibility |
|---|---:|---|
| `auth-service` | `8000` | Authenticates users and generates JWT access tokens |
| `order-service` | `8001` | Coordinates the distributed TCC transaction |
| `inventory-service` | `8002` | Reserves, confirms, or releases product quantities |
| `payment-service` | `8003` | Blocks, charges, or releases user funds |
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
- payment increases the account's `blocked` amount;
- shipping creates a shipment in the `RESERVED` state.

A successful Try means:

> The participant guarantees that the resource is reserved and can later be either confirmed or cancelled.

### Confirm

If every Try succeeds, the coordinator decides `COMMIT` and asks every participant to make its reservation final.

In this project:

- inventory decreases both `reserved` and `stock`;
- payment decreases both `blocked` and `balance`;
- shipping changes the shipment state to `CONFIRMED`.

After the coordinator has persisted the `COMMIT` decision, a Confirm failure does **not** cause a rollback. The coordinator keeps the `COMMIT` decision and retries the missing Confirm operations.

### Cancel

If at least one Try fails before the commit decision, the coordinator decides `ROLLBACK` and asks the participants to release any reserved resources.

In this project:

- inventory decreases `reserved` without changing `stock`;
- payment decreases `blocked` without changing `balance`;
- shipping changes the shipment state to `CANCELLED`.

The coordinator sends Cancel to every participant. Participants that never created a reservation return a successful empty-cancel response.

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
├── auth-service/
│   ├── main.py
│   ├── requirements.txt
│   └── Dockerfile
│
├── order-service/
│   ├── main.py
│   ├── requirements.txt
│   └── Dockerfile
│
├── inventory-service/
│   ├── main.py
│   ├── requirements.txt
│   └── Dockerfile
│
├── payment-service/
│   ├── main.py
│   ├── requirements.txt
│   └── Dockerfile
│
├── shipping-service/
│   ├── main.py
│   ├── requirements.txt
│   └── Dockerfile
│
├── postman/
│   ├── Distributed_TCC_Complete_Test_Suite.postman_collection.json
│   └── Distributed_TCC_Local.postman_environment.json
│
├── docker-compose.yml
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
- Docker
- Docker Compose
- Postman

---

## Requirements

To run the project, install:

- Docker;
- Docker Compose;
- Postman, optionally, for API testing.

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

The Compose configuration provides development defaults.

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

The `order-service` verifies the token signature, expiration, and the presence of the `sub` claim before starting the distributed transaction.

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
POST /tcc/try
PUT  /tcc/confirm
PUT  /tcc/cancel
```

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

### Payment state

An account contains:

```text
balance
blocked
```

During Try:

```text
balance unchanged
blocked increased
```

After Confirm:

```text
balance decreased
blocked decreased
```

After Cancel:

```text
balance unchanged
blocked decreased
```

### Shipping state

A shipment moves between:

```text
RESERVED
CONFIRMED
CANCELLED
```

---

## Testing with Postman

The `postman/` directory contains:

```text
Distributed_TCC_Complete_Test_Suite.postman_collection.json
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

The direct participant tests use fixed transaction IDs. Before repeating the entire suite, either:

- change the variables `inventoryTx`, `paymentTx`, and `shippingTx`;
- or restart the participant containers to reset their in-memory state.

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

Because shipping is unavailable, its Cancel also fails.

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
| Shipping Try fails | `ROLLBACK` | `CANCELLED` or `CANCEL_PENDING` |
| Confirm temporarily fails after commit | `COMMIT` | `CONFIRM_PENDING` |
| Cancel temporarily fails after rollback | `ROLLBACK` | `CANCEL_PENDING` |
| Coordinator restarts during Try | `ROLLBACK` during recovery | `CANCELLED` or `CANCEL_PENDING` |
| Coordinator restarts after commit | `COMMIT` remains unchanged | `CONFIRMED` or `CONFIRM_PENDING` |

---

## Limitations

This is a didactic implementation.

The following limitations are intentional:

- inventory, payment, and shipping keep their state in memory;
- participant state is lost when the corresponding container restarts;
- only the order coordinator transaction log is persisted;
- concurrent updates are not synchronized with locks or database transactions;
- monetary values use floating-point numbers;
- internal participant endpoints are exposed on host ports for testing;
- participant endpoints do not use service-to-service authentication;
- passwords are hard-coded and stored in plain text;
- the development JWT secret has a Compose default;
- retry uses a fixed delay rather than exponential backoff;
- the application does not include distributed tracing or metrics;
- `GET /orders` and diagnostic state endpoints are not access-controlled.

### Recovery scope

Recovery is reliable for coordinator restarts while participant containers retain their in-memory state.

If a participant restarts after creating a reservation, it loses that reservation and may return `404` to a later Confirm.

A production implementation would persist participant state in databases.

---

## Possible Improvements

Possible future improvements include:

- persistent databases for every participant;
- transactional state updates;
- optimistic or pessimistic concurrency control;
- monetary amounts represented with `Decimal` or integer cents;
- asymmetric JWT signing;
- service-to-service authentication;
- protected diagnostic endpoints;
- exponential backoff with jitter;
- background recovery worker;
- reservation expiration and reconciliation;
- structured logging;
- correlation IDs;
- OpenTelemetry distributed tracing;
- Prometheus metrics;
- automated integration tests;
- health checks and readiness checks in Docker Compose;
- dependency version locking;
- CI/CD pipeline;
- secret management outside the repository.

---

## Educational Summary

The core rule demonstrated by this project is:

> A failure during Try allows the coordinator to decide ROLLBACK and execute Cancel.  
> After the coordinator persists COMMIT, a later Confirm failure must be retried and must not be converted into rollback.

The transaction decision indicates **where the distributed transaction must arrive**.

The transaction status indicates **how far the coordinator has progressed toward that result**.
