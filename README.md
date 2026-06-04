# Distributed System with JWT and TCC

Microservices-based distributed order management system with JWT authentication and Try-Confirm/Cancel transactions.

## Overview

This project implements a distributed system based on independent microservices.
The system simulates the creation of an order that involves multiple services:

- authentication service
- order coordinator service
- inventory service
- payment service
- shipping service

The order creation process is coordinated using the **Try-Confirm/Cancel** pattern.

The goal of the project is to show typical concepts of distributed systems, such as:

- communication between independent services
- JWT-based authentication
- distributed transaction coordination
- partial failure management
- compensation actions
- separation of state between services
- containerized deployment with Docker Compose

## Architecture

The system is composed of the following services:

| Service             |   Port | Description                                 |
| ------------------- | -----: | ------------------------------------------- |
| `auth-service`      | `8000` | Handles login and JWT generation            |
| `order-service`     | `8001` | Coordinates the distributed TCC transaction |
| `inventory-service` | `8002` | Reserves, confirms or cancels product stock |
| `payment-service`   | `8003` | Blocks, confirms or cancels payments        |
| `shipping-service`  | `8004` | Prepares, confirms or cancels shipments     |

Each service is implemented as an independent FastAPI application and runs inside its own Docker container.

## TCC Pattern

The Try-Confirm/Cancel pattern is used to coordinate a distributed transaction across multiple services.

The transaction is divided into three phases:

### Try

Each participant reserves the required resource without applying the final change.

Example:

- inventory reserves product quantity
- payment blocks the amount
- shipping prepares the shipment

### Confirm

If all Try operations succeed, the coordinator confirms all reserved operations.

Example:

- inventory decreases the stock
- payment confirms the charge
- shipping confirms the shipment

### Cancel

If one Try operation fails, the coordinator cancels the operations that already succeeded.

Example:

- inventory releases the reserved stock
- payment releases the blocked amount
- shipping cancels the prepared shipment

## Order Flow

The `order-service` acts as the coordinator.

When a client creates an order, the following flow is executed:

```text
TRY inventory
TRY payment
TRY shipping

if all TRY operations succeed:
    CONFIRM inventory
    CONFIRM payment
    CONFIRM shipping
    order = CONFIRMED

if one TRY operation fails:
    CANCEL already completed steps
    order = CANCELLED
```

## Project Structure

```text
distributed-tcc-orders/
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
│   └── Distributed-TCC-Orders.postman_collection.json
│
├── docker-compose.yml
└── README.md
```

## Technologies Used

- Python
- FastAPI
- Uvicorn
- HTTPX
- JWT
- Docker
- Docker Compose
- Postman

## How to Run

From the root directory of the project, run:

```bash
docker compose up --build
```

The services will be available at:

```text
auth-service        http://localhost:8000
order-service       http://localhost:8001
inventory-service   http://localhost:8002
payment-service     http://localhost:8003
shipping-service    http://localhost:8004
```

Each FastAPI service also provides automatic Swagger documentation:

```text
http://localhost:8000/docs
http://localhost:8001/docs
http://localhost:8002/docs
http://localhost:8003/docs
http://localhost:8004/docs
```

## Authentication

The system uses JWT authentication.

The client must first log in through the `auth-service`.

### Login Endpoint

```http
POST http://localhost:8000/login
```

Request body:

```json
{
  "username": "gabriele",
  "password": "password"
}
```

Response example:

```json
{
  "access_token": "eyJhbGciOiJIUzI1NiIs...",
  "token_type": "bearer"
}
```

The returned token must be sent to the `order-service` using the `Authorization` header:

```http
Authorization: Bearer <access_token>
```

## Main Endpoints

### Auth Service

```http
GET  /health
POST /login
```

### Order Service

```http
GET  /health
GET  /orders
GET  /orders/{transaction_id}
POST /orders
```

### Inventory Service

```http
GET  /health
GET  /state
GET  /products/{product_id}
POST /tcc/try
PUT  /tcc/confirm
PUT  /tcc/cancel
```

### Payment Service

```http
GET  /health
GET  /state
POST /tcc/try
PUT  /tcc/confirm
PUT  /tcc/cancel
```

### Shipping Service

```http
GET  /health
GET  /state
POST /tcc/try
PUT  /tcc/confirm
PUT  /tcc/cancel
```

## Example: Successful Order

After obtaining a JWT token, send a request to:

```http
POST http://localhost:8001/orders
```

Headers:

```http
Authorization: Bearer <access_token>
Content-Type: application/json
```

Body:

```json
{
  "product_id": "p1",
  "quantity": 2,
  "address": "Via Roma 1",
  "fail_payment": false,
  "fail_shipping": false
}
```

Expected result:

```json
{
  "status": "ORDER_CONFIRMED",
  "transaction_id": "...",
  "completed_steps": ["inventory", "payment", "shipping"]
}
```

In this case:

```text
TRY inventory  -> OK
TRY payment    -> OK
TRY shipping   -> OK

CONFIRM inventory  -> OK
CONFIRM payment    -> OK
CONFIRM shipping   -> OK
```

The order is confirmed.

## Example: Payment Failure

To simulate a payment failure, send:

```json
{
  "product_id": "p1",
  "quantity": 2,
  "address": "Via Roma 1",
  "fail_payment": true,
  "fail_shipping": false
}
```

Expected behavior:

```text
TRY inventory  -> OK
TRY payment    -> FAIL

CANCEL inventory -> OK
order = CANCELLED
```

This demonstrates compensation after a partial failure.

## Example: Shipping Failure

To simulate a shipping failure, send:

```json
{
  "product_id": "p1",
  "quantity": 2,
  "address": "Via Roma 1",
  "fail_payment": false,
  "fail_shipping": true
}
```

Expected behavior:

```text
TRY inventory  -> OK
TRY payment    -> OK
TRY shipping   -> FAIL

CANCEL payment   -> OK
CANCEL inventory -> OK
order = CANCELLED
```

This demonstrates rollback of multiple already completed Try steps.

## State Inspection

The internal state of each service can be inspected using the following endpoints:

```http
GET http://localhost:8001/orders
GET http://localhost:8002/state
GET http://localhost:8003/state
GET http://localhost:8004/state
```

These endpoints are useful to verify the effects of the TCC protocol.

For example:

- after a successful order, inventory stock is decreased, payment is confirmed and shipping is confirmed
- after a failed order, reserved resources are cancelled and released

## Testing with Postman

A Postman collection is available in the `postman/` folder:

```text
postman/Distributed-TCC-Orders.postman_collection.json
```

No Postman environment is required.

The JWT token is stored as a **collection variable** named `token`.

### How to test with Postman

1. Start the system:

```bash
docker compose up --build
```

2. Open Postman.

3. Import the collection from the `postman/` folder.

4. Run the `Login` request first.

5. The JWT returned by `auth-service` is automatically saved in the collection variable `token`.

6. Run one of the order requests:

```text
Create Order - Success
Create Order - Payment Failure
Create Order - Shipping Failure
Create Order - No Token
```

7. Use the state requests to inspect the system:

```text
Get Orders State
Get Inventory State
Get Payment State
Get Shipping State
```

## Suggested Demo Flow

A possible demonstration flow is:

```text
1. Start all services with Docker Compose
2. Run Login
3. Run Create Order - No Token
   Expected result: 401 Unauthorized
4. Run Create Order - Success
   Expected result: ORDER_CONFIRMED
5. Inspect inventory, payment and shipping state
6. Run Create Order - Payment Failure
   Expected result: order CANCELLED and inventory rollback
7. Inspect orders and inventory state
8. Run Create Order - Shipping Failure
   Expected result: order CANCELLED, payment rollback and inventory rollback
9. Inspect all service states
```

## Distributed Systems Concepts Shown

This project demonstrates several distributed systems concepts:

### Independent Services

Each service is an independent process running in a separate container.

### Network Communication

Services communicate through HTTP REST APIs.

### No Shared Memory

Each service owns its own local state.

### Partial Failures

One service may fail while the others have already completed their Try phase.

### Compensation

The coordinator uses Cancel operations to undo already reserved resources.

### Eventual Consistency at Application Level

During the transaction, services may temporarily hold intermediate states.
The system reaches a final consistent state after Confirm or Cancel.

### Authentication in a Distributed System

The `auth-service` generates a JWT, and the `order-service` verifies it before starting the distributed transaction.

## Notes

This project is a didactic implementation.

The services currently store their state in memory, so data is reset when containers are restarted.

The goal is not to implement a production-ready order management system, but to demonstrate the coordination of distributed services using JWT and the Try-Confirm/Cancel pattern.
