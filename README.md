# aws-container — Flask + Docker + AWS ECS/Fargate

A hands-on learning project. A small **Flask** web app lets you upload a CSV of
customer IDs and hours worked. It stores them in **PostgreSQL** using
**SQLAlchemy**, inserting new customers and **cumulatively updating** existing
ones (no duplicate rows). You'll containerize it with **Docker**, run it locally
with **docker compose**, and deploy it to **AWS ECS on Fargate** behind an
**Application Load Balancer**, with a **GitHub Actions** CI/CD pipeline.

> **Region used throughout:** `us-east-2` (Ohio). Public access is via the ALB's
> DNS name — no custom domain required.

---

## 📑 Table of Contents

- [aws-container — Flask + Docker + AWS ECS/Fargate](#aws-container--flask--docker--aws-ecsfargate)
  - [📑 Table of Contents](#-table-of-contents)
  - [🏛 Architecture](#-architecture)
  - [📁 Project structure](#-project-structure)
  - [⚙️ How the app works](#️-how-the-app-works)
  - [🖥 Run it locally](#-run-it-locally)
  - [🧪 Local testing \& debugging](#-local-testing--debugging)
  - [☁️ Deploy to AWS](#️-deploy-to-aws)
  - [🔁 CI/CD with GitHub Actions](#-cicd-with-github-actions)
  - [🔍 AWS debugging \& monitoring commands](#-aws-debugging--monitoring-commands)
  - [🧹 Tear everything down](#-tear-everything-down)
  - [✅ Best practices for containerized Flask](#-best-practices-for-containerized-flask)
  - [🎓 Interview preparation](#-interview-preparation)

---

## 🏛 Architecture

**Local (docker compose):**

```
Browser ──> Flask (gunicorn) container :5000 ──> PostgreSQL container :5432
                                                     │
                                              pgdata volume (persistent)
```

**AWS (production):**

```
                 Internet
                    │
        ┌───────────▼────────────┐
        │  Application Load      │   (public subnets, public DNS)
        │  Balancer  (ALB) :80   │
        └───────────┬────────────┘
                    │  target group -> /health
        ┌───────────▼────────────┐
        │ ECS Service on Fargate │   (private/public subnets)
        │  Task: Flask container │
        └───────────┬────────────┘
                    │  :5432
        ┌───────────▼────────────┐
        │ Amazon RDS PostgreSQL  │   (private, security-group locked)
        └────────────────────────┘

Images live in Amazon ECR. GitHub Actions builds & deploys on every push to main.
```

**Why each piece exists:**

| Component | Why it's needed |
|---|---|
| **Docker** | Packages the app + dependencies so it runs identically everywhere. |
| **docker compose** | One command spins up app + DB locally for development. |
| **Amazon ECR** | Private registry that stores your Docker images for ECS to pull. |
| **ECS + Fargate** | Runs containers without you managing any servers (serverless containers). |
| **Amazon RDS** | Managed PostgreSQL — backups, patching, availability handled by AWS. |
| **ALB** | Public entry point; load-balances traffic and runs health checks. |
| **IAM roles** | Grant least-privilege permissions to pull images, write logs, run tasks. |
| **Security groups** | Virtual firewalls controlling who can talk to the ALB, tasks, and DB. |
| **GitHub Actions** | Automates build → push → deploy so you never do it by hand. |

---

## 📁 Project structure

```
aws-container/
├── app.py                     # Flask application (upload, upsert, view)
├── requirements.txt           # Python deps (uv-compatible)
├── Dockerfile                 # Multi-stage build (uv + gunicorn, non-root)
├── docker-compose.yml         # Flask + PostgreSQL with a persistent volume
├── .dockerignore              # Keeps build context small & secret-free
├── .env.example               # Copy to .env for local config
├── .gitignore
├── README.md                  # You are here
├── AWS_DEPLOYMENT.md          # Step-by-step AWS setup (beginner friendly)
├── TEARDOWN.md                # Delete every AWS resource to stop costs
├── sample_customers.csv       # 10 sample records to try
├── templates/
│   ├── index.html             # Drag & drop upload UI
│   └── view_data.html         # Table of all customers
└── .github/
    └── workflows/
        └── deploy.yml         # CI/CD: build -> ECR -> ECS
```

---

## ⚙️ How the app works

- **CSV format:** header `Customer ID,Hours Worked` (header optional; first two
  columns are used if no header is detected).
- **Customer ID** must be exactly **8 digits**. Invalid rows are skipped and
  reported, not fatal.
- **Upsert logic:** if the customer id already exists, the new hours are **added**
  to the stored total (cumulative). Otherwise a new row is inserted. This
  guarantees **no duplicate customers**.
- **Routes:**
  - `GET /` — upload page
  - `POST /upload` — process the CSV, show a summary (inserted / updated / skipped)
  - `GET /data` — table of all customers + totals
  - `GET /health` — JSON health check used by Docker and the ALB target group

---

## 🖥 Run it locally

**Prerequisites:** Docker Desktop (Windows/Mac/Linux) running.

```bash
# 1. Get the code
git clone https://github.com/<your-username>/aws-container.git
cd aws-container

# 2. Create your local env file
cp .env.example .env        # Windows: copy .env.example .env

# 3. Build and start everything
docker compose up --build
```

Open **http://localhost:5000**. Upload `sample_customers.csv`, then click
**View All Data**. Upload it a second time and watch the hours **double** — that
demonstrates the cumulative upsert.

Stop it:

```bash
docker compose down          # stop containers, keep data
docker compose down -v       # stop AND delete the pgdata volume (fresh start)
```

> **Note on localhost:** these `localhost` URLs refer to the machine actually
> running Docker. If you're running the stack on a remote/VM host, use that
> host's address or the platform-provided preview URL instead.

---

## 🧪 Local testing & debugging

```bash
# See running containers (like the AWS "what's running?" question)
docker compose ps

# Tail application logs
docker compose logs -f web
docker compose logs -f db

# Open a shell inside the web container
docker compose exec web sh

# Open a psql prompt inside the database
docker compose exec db psql -U appuser -d customers -c "SELECT * FROM customers;"

# Inspect low-level container details (networks, mounts, env)
docker inspect aws_container_web

# Hit the health endpoint
curl http://localhost:5000/health
```

---

## ☁️ Deploy to AWS

Full, click-by-click, beginner-friendly instructions live in
**[AWS_DEPLOYMENT.md](AWS_DEPLOYMENT.md)**. High-level flow:

1. Create an **IAM user** for CLI/CI + the two **ECS roles**.
2. Create an **ECR repository** and push your first image.
3. Create an **RDS PostgreSQL** instance.
4. Create the **VPC security groups** (ALB, ECS task, RDS).
5. Create the **ALB + target group**.
6. Create the **ECS cluster, task definition, and service** on Fargate.
7. Open the ALB DNS name in your browser. 🎉

---

## 🔁 CI/CD with GitHub Actions

The workflow in `.github/workflows/deploy.yml` runs on every push to `main`:
**build image → push to ECR → render task definition → deploy to ECS**.

Add these repository secrets (**Settings → Secrets and variables → Actions →
New repository secret**):

| Secret | Value |
|---|---|
| `AWS_ACCESS_KEY_ID` | Access key of the deploy IAM user |
| `AWS_SECRET_ACCESS_KEY` | Its secret access key |

The non-secret settings (region, cluster, service, repo names) are defined in the
`env:` block at the top of `deploy.yml` — make sure they match the names you
create in AWS. See AWS_DEPLOYMENT.md for the exact IAM policy the deploy user
needs.

---

## 🔍 AWS debugging & monitoring commands

These are the AWS equivalents of the Docker commands you use locally. Full
explanations are in AWS_DEPLOYMENT.md.

| Local Docker | AWS equivalent |
|---|---|
| `docker ps` | `aws ecs list-tasks --cluster aws-container-cluster` |
| `docker logs <c>` | `aws logs tail /ecs/aws-container-task --follow` |
| `docker exec -it <c> sh` | `aws ecs execute-command ... --command "/bin/sh" --interactive` |
| `docker inspect <c>` | `aws ecs describe-tasks --cluster ... --tasks <arn>` |

```bash
# Which tasks are running?
aws ecs list-tasks --cluster aws-container-cluster --region us-east-2

# Live application logs
aws logs tail /ecs/aws-container-task --follow --region us-east-2

# Service status, events, deployment rollout
aws ecs describe-services --cluster aws-container-cluster \
  --services aws-container-service --region us-east-2
```

---

## 🧹 Tear everything down

AWS charges by the hour for RDS, the ALB, and NAT/data. When you're done
learning, follow **[TEARDOWN.md](TEARDOWN.md)** to delete **every** resource in
the right order so you stop incurring costs.

---

## ✅ Best practices for containerized Flask

- **Use a production WSGI server** (gunicorn) — never `flask run`/`app.run()` in prod.
- **Multi-stage builds** keep images small and free of compilers.
- **Run as a non-root user** inside the container (this image uses `appuser`).
- **Externalize config** via environment variables; never bake secrets into images.
- **Add a `/health` endpoint** for the ALB and container health checks.
- **`.dockerignore`** to avoid shipping `.git`, `.env`, and docs into the image.
- **Pin dependency versions** for reproducible builds.
- **Stateless containers** — all persistent state lives in RDS, so tasks can be
  killed/replaced freely (essential for scaling and rolling deploys).
- **Log to stdout/stderr** so CloudWatch captures everything automatically.
- **Least-privilege IAM** — separate execution role (pull image, write logs) from
  task role (app's own AWS permissions).

---

## 🎓 Interview preparation

A dedicated section covering **observability, scalability, latency, distributed
tracing, high availability, cost optimization, and resource monitoring** — mapped
to this exact architecture — is at the bottom of **[AWS_DEPLOYMENT.md](AWS_DEPLOYMENT.md#-interview-preparation)**.
