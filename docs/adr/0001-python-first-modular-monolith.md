# ADR 0001: Python-first modular monolith

- Status: Accepted
- Date: 2026-08-18

## Decision

Dogfood V1 uses a Python-first modular monolith:

- Django 5.2 LTS;
- PostgreSQL as the authoritative runtime database;
- Django templates and minimal browser JavaScript;
- Docker for repeatable packaging;
- local filesystem storage behind a configurable boundary during development;
- object storage selected before Staging;
- manual external-platform publishing only.

SQLite is permitted only as a local lightweight test/preview mode while Docker/PostgreSQL is unavailable. It is not an accepted Staging or Production runtime and cannot prove PostgreSQL-specific constraints.

## Why

The V1 risk is workflow correctness: identity, permissions, sealed versions, checks, human review, fail-closed release gates, audit and recovery. A single Django application reduces authentication, transaction and deployment boundaries while preserving modular domains. Python also supports the later demand-intelligence, collection and AI workloads without introducing a second backend runtime now.

## Deferred

- Separate Next.js or other TypeScript frontend.
- FastAPI/microservice extraction.
- Automatic connectors, model routing and platform publishing.
- Tencent Cloud service-specific adapters until the Staging decision gate.

## Consequences

- All cloud-specific values are environment configuration.
- Production fails to start without explicit secrets, hosts and PostgreSQL configuration.
- Domain modules may be extracted later, but V1 does not pay the cost of distributed authentication, API contracts or multiple deployments.

