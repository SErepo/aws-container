# AWS Deployment Guide — ECS/Fargate + RDS + ALB (Region: us-east-2)

This is a **complete, beginner-friendly** walkthrough to deploy the `aws-container`
Flask app to AWS. Every step explains **what** you're doing and **why**. You can
use the **AWS Console** (point-and-click) or the **AWS CLI** — both are shown.

> 💡 **Cost warning:** RDS, the ALB, and Fargate tasks cost money by the hour
> (a few US$ per day for this setup). When finished, follow **[TEARDOWN.md](TEARDOWN.md)**.

---

## 📋 Contents

1. [Prerequisites](#0-prerequisites)
2. [Core concepts & vocabulary](#1-core-concepts)
3. [IAM: user + roles + policies](#2-iam-setup)
4. [ECR: create repo & push image](#3-ecr-create-repository--push-image)
5. [Networking: VPC & security groups](#4-networking--security-groups)
6. [RDS: PostgreSQL database](#5-rds-postgresql-database)
7. [ALB: load balancer & target group](#6-application-load-balancer)
8. [ECS: cluster, task definition, service](#7-ecs-cluster-task-definition--service)
9. [Verify it works](#8-verify)
10. [AWS debugging commands (docker ps/logs/exec/inspect)](#9-aws-debugging--monitoring)
11. [Interview preparation](#-interview-preparation)

---

## 0. Prerequisites

- A new **AWS account** (root email verified, billing set up).
- **AWS CLI v2** installed: `aws --version`. Configure it after creating the IAM
  user below with `aws configure` (region `us-east-2`, output `json`).
- **Docker Desktop** running locally.
- Your **AWS Account ID** (12 digits) — find it in the console top-right menu.
  We'll refer to it as `<ACCOUNT_ID>` throughout.

Set some shell variables to make copy-paste easy (Mac/Linux/Git-Bash):

```bash
export AWS_REGION=us-east-2
export ACCOUNT_ID=<your-12-digit-account-id>
export APP=aws-container
```

---

## 1. Core concepts

| Term | Plain-English meaning |
|---|---|
| **VPC** | Your private network in AWS. Comes with subnets in multiple AZs. |
| **Subnet** | A slice of the VPC in one Availability Zone. *Public* subnets can reach the internet; *private* ones can't directly. |
| **Availability Zone (AZ)** | An isolated datacenter. Using ≥2 gives high availability. |
| **Security Group (SG)** | A stateful firewall attached to resources; you allow specific ports/sources. |
| **ECR** | Elastic Container Registry — private Docker Hub for your images. |
| **ECS** | Elastic Container Service — orchestrates containers. |
| **Fargate** | Serverless compute for ECS: you specify CPU/RAM, AWS runs the container, no EC2 to manage. |
| **Task definition** | The "recipe" for a container: image, CPU, memory, ports, env vars, roles, logs. |
| **Task** | A running instance of a task definition. |
| **Service** | Keeps N tasks running, replaces unhealthy ones, and registers them with the ALB. |
| **ALB** | Application Load Balancer — public HTTP entry point with health checks. |
| **Target group** | The set of tasks the ALB routes to, plus the health-check rule. |
| **RDS** | Relational Database Service — managed PostgreSQL. |

---

## 2. IAM setup

**Why:** IAM controls *who* and *what* can act in your account. We create (a) a
**deploy user** for the CLI and GitHub Actions, and (b) two **roles** that ECS
tasks assume at runtime. Using least privilege limits blast radius if a key leaks.

### 2a. Deploy user (for CLI + GitHub Actions)

Console: **IAM → Users → Create user** → name `aws-container-deployer` →
**Attach policies directly**. For learning you may attach these AWS-managed
policies (tighten later):

- `AmazonEC2ContainerRegistryPowerUser` (push/pull ECR)
- `AmazonECS_FullAccess` (manage ECS)
- `CloudWatchLogsFullAccess` (read logs)

Then **Create access key → Command Line Interface (CLI)**. Save the
**Access key ID** and **Secret access key** — these become your GitHub secrets
`AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY`.

Configure your local CLI:

```bash
aws configure
# AWS Access Key ID:     <paste>
# AWS Secret Access Key: <paste>
# Default region name:   us-east-2
# Default output format:  json
aws sts get-caller-identity   # confirms it works
```

> **Tighter (recommended) policy** for the deploy user, once you're comfortable —
> a custom policy limited to `ecr:*` on your repo, `ecs:UpdateService`,
> `ecs:RegisterTaskDefinition`, `ecs:DescribeServices/Tasks/TaskDefinition`,
> and `iam:PassRole` for the two roles below.

### 2b. ECS task execution role

**Why:** lets ECS/Fargate **pull the image from ECR** and **write logs to
CloudWatch** on your behalf while starting the task.

```bash
cat > trust.json <<'EOF'
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Principal": { "Service": "ecs-tasks.amazonaws.com" },
    "Action": "sts:AssumeRole"
  }]
}
EOF

aws iam create-role --role-name ecsTaskExecutionRole \
  --assume-role-policy-document file://trust.json

aws iam attach-role-policy --role-name ecsTaskExecutionRole \
  --policy-arn arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy
```

*(If `ecsTaskExecutionRole` already exists in your account, skip creation.)*

### 2c. ECS task role

**Why:** the permissions **your application code** has at runtime. This app only
talks to RDS over the network, so it needs **no** AWS API permissions — but we
create an empty role for good hygiene (and for ECS Exec later).

```bash
aws iam create-role --role-name ecsTaskRole \
  --assume-role-policy-document file://trust.json

# Enable "ECS Exec" (the AWS equivalent of `docker exec`) by allowing SSM messages:
aws iam attach-role-policy --role-name ecsTaskRole \
  --policy-arn arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore
```

---

## 3. ECR: create repository & push image

**Why:** Fargate can only run images from a registry it can reach. ECR is the
private, IAM-secured registry.

```bash
# Create the repo
aws ecr create-repository --repository-name $APP --region $AWS_REGION

# Authenticate Docker to ECR
aws ecr get-login-password --region $AWS_REGION \
  | docker login --username AWS --password-stdin \
    $ACCOUNT_ID.dkr.ecr.$AWS_REGION.amazonaws.com

# Build, tag, push
docker build -t $APP .
docker tag $APP:latest $ACCOUNT_ID.dkr.ecr.$AWS_REGION.amazonaws.com/$APP:latest
docker push $ACCOUNT_ID.dkr.ecr.$AWS_REGION.amazonaws.com/$APP:latest
```

You now have an image URI:
`<ACCOUNT_ID>.dkr.ecr.us-east-2.amazonaws.com/aws-container:latest`.

---

## 4. Networking & security groups

We'll use your account's **default VPC** (already has public subnets in multiple
AZs — perfect for learning). Get the IDs:

```bash
# Default VPC id
export VPC_ID=$(aws ec2 describe-vpcs --filters Name=isDefault,Values=true \
  --query 'Vpcs[0].VpcId' --output text --region $AWS_REGION)

# Subnets in that VPC (pick at least two, in different AZs)
aws ec2 describe-subnets --filters Name=vpc-id,Values=$VPC_ID \
  --query 'Subnets[].{Id:SubnetId,AZ:AvailabilityZone}' --output table --region $AWS_REGION
```

Sample response:
--------------------------------------------
|              DescribeSubnets             |
+-------------+----------------------------+
|     AZ      |            Id              |
+-------------+----------------------------+
|  us-east-2a |  subnet-09c323de15d564413  |
|  us-east-2b |  subnet-002e474f9d2c7f55f  |
|  us-east-2c |  subnet-01f26371679a3cbf7  |
+-------------+----------------------------+


Create **three security groups** (layered firewalls):

```bash
# (1) ALB SG — allow HTTP from the whole internet
export ALB_SG=$(aws ec2 create-security-group --group-name ${APP}-alb-sg \
  --description "ALB HTTP in" --vpc-id $VPC_ID --region $AWS_REGION \
  --query 'GroupId' --output text)
aws ec2 authorize-security-group-ingress --group-id $ALB_SG \
  --protocol tcp --port 80 --cidr 0.0.0.0/0 --region $AWS_REGION

# (2) ECS task SG — allow 5000 ONLY from the ALB SG
export TASK_SG=$(aws ec2 create-security-group --group-name ${APP}-task-sg \
  --description "App from ALB" --vpc-id $VPC_ID --region $AWS_REGION \
  --query 'GroupId' --output text)
aws ec2 authorize-security-group-ingress --group-id $TASK_SG \
  --protocol tcp --port 5000 --source-group $ALB_SG --region $AWS_REGION

# (3) RDS SG — allow 5432 ONLY from the ECS task SG
export RDS_SG=$(aws ec2 create-security-group --group-name ${APP}-rds-sg \
  --description "Postgres from tasks" --vpc-id $VPC_ID --region $AWS_REGION \
  --query 'GroupId' --output text)
aws ec2 authorize-security-group-ingress --group-id $RDS_SG \
  --protocol tcp --port 5432 --source-group $TASK_SG --region $AWS_REGION
```

**Why layered:** the internet can only reach the ALB; only the ALB can reach the
app; only the app can reach the database. This is defense in depth.

---

## 5. RDS PostgreSQL database

**Why:** a managed database so you don't run/patch Postgres yourself. For
learning we keep it small (`db.t3.micro`).

**Console:** RDS → **Create database** → *Standard create* → **PostgreSQL** →
Template **Free tier** (or Dev/Test) → set:
- DB instance identifier: `aws-container-db`
- Master username: `appuser`, master password: *(choose a strong one)*
- Instance: `db.t3.micro`, storage 20 GB
- **Connectivity:** default VPC, **Public access = No**, VPC security group =
  **existing → `aws-container-rds-sg`**
- Additional config → **Initial database name: `customers`**

**CLI equivalent:**

```bash
aws rds create-db-instance \
  --db-instance-identifier aws-container-db \
  --engine postgres --engine-version 16 \
  --db-instance-class db.t3.micro \
  --allocated-storage 20 \
  --master-username appuser \
  --master-user-password 'ChangeThisStrongPass1!' \
  --db-name customers \
  --vpc-security-group-ids $RDS_SG \
  --no-publicly-accessible \
  --backup-retention-period 1 \
  --region $AWS_REGION

# Wait, then get the endpoint host:
aws rds wait db-instance-available --db-instance-identifier aws-container-db --region $AWS_REGION
aws rds describe-db-instances --db-instance-identifier aws-container-db \
  --query 'DBInstances[0].Endpoint.Address' --output text --region $AWS_REGION
```

Save that endpoint — your `DATABASE_URL` will be:
`postgresql://appuser:<password>@<endpoint>:5432/customers`

---

## 6. Application Load Balancer

**Why:** gives you a stable public DNS name, spreads traffic across tasks/AZs,
and health-checks tasks so unhealthy ones are replaced.

```bash
# Two subnet IDs from step 4 (different AZs):
export SUBNET_A=<subnet-xxxx>
export SUBNET_B=<subnet-yyyy>

# Create the ALB
export ALB_ARN=$(aws elbv2 create-load-balancer --name ${APP}-alb \
  --subnets $SUBNET_A $SUBNET_B --security-groups $ALB_SG \
  --scheme internet-facing --type application --region $AWS_REGION \
  --query 'LoadBalancers[0].LoadBalancerArn' --output text)

# Target group (type ip is required for Fargate) with health check on /health
export TG_ARN=$(aws elbv2 create-target-group --name ${APP}-tg \
  --protocol HTTP --port 5000 --vpc-id $VPC_ID --target-type ip \
  --health-check-path /health --health-check-interval-seconds 30 \
  --region $AWS_REGION --query 'TargetGroups[0].TargetGroupArn' --output text)

# Listener: forward :80 -> target group
aws elbv2 create-listener --load-balancer-arn $ALB_ARN \
  --protocol HTTP --port 80 \
  --default-actions Type=forward,TargetGroupArn=$TG_ARN --region $AWS_REGION

# The public DNS name to visit later:
aws elbv2 describe-load-balancers --load-balancer-arns $ALB_ARN \
  --query 'LoadBalancers[0].DNSName' --output text --region $AWS_REGION
```

---

## 7. ECS cluster, task definition & service

### 7a. Log group + cluster

```bash
aws logs create-log-group --log-group-name /ecs/${APP}-task --region $AWS_REGION
aws ecs create-cluster --cluster-name ${APP}-cluster --region $AWS_REGION
```

### 7b. Task definition

Create `task-definition.json` (replace `<ACCOUNT_ID>`, the RDS endpoint, and the
password). This mirrors the container we tested locally.

```json
{
  "family": "aws-container-task",
  "networkMode": "awsvpc",
  "requiresCompatibilities": ["FARGATE"],
  "cpu": "512",
  "memory": "1024",
  "executionRoleArn": "arn:aws:iam::<ACCOUNT_ID>:role/ecsTaskExecutionRole",
  "taskRoleArn": "arn:aws:iam::<ACCOUNT_ID>:role/ecsTaskRole",
  "containerDefinitions": [
    {
      "name": "aws-container-web",
      "image": "<ACCOUNT_ID>.dkr.ecr.us-east-2.amazonaws.com/aws-container:latest",
      "essential": true,
      "portMappings": [{ "containerPort": 5000, "protocol": "tcp" }],
      "environment": [
        { "name": "DATABASE_URL", "value": "postgresql://appuser:ChangeThisStrongPass1!@<RDS_ENDPOINT>:5432/customers" },
        { "name": "SECRET_KEY", "value": "replace-with-long-random-string" }
      ],
      "logConfiguration": {
        "logDriver": "awslogs",
        "options": {
          "awslogs-group": "/ecs/aws-container-task",
          "awslogs-region": "us-east-2",
          "awslogs-stream-prefix": "web"
        }
      }
    }
  ]
}
```

> **Production tip:** store the DB password in **AWS Secrets Manager** and
> reference it via the task definition's `secrets` block instead of plaintext
> `environment`. Plaintext is used here only to keep the learning path simple.

Register it:

```bash
aws ecs register-task-definition --cli-input-json file://task-definition.json --region $AWS_REGION
```

### 7c. Service (wired to the ALB)

```bash
aws ecs create-service \
  --cluster ${APP}-cluster \
  --service-name ${APP}-service \
  --task-definition aws-container-task \
  --desired-count 1 \
  --launch-type FARGATE \
  --enable-execute-command \
  --network-configuration "awsvpcConfiguration={subnets=[$SUBNET_A,$SUBNET_B],securityGroups=[$TASK_SG],assignPublicIp=ENABLED}" \
  --load-balancers "targetGroupArn=$TG_ARN,containerName=${APP}-web,containerPort=5000" \
  --region $AWS_REGION
```

- `assignPublicIp=ENABLED` lets the task pull the image from ECR in the default
  VPC without a NAT gateway (cheaper for learning).
- `--enable-execute-command` turns on **ECS Exec** (the `docker exec` equivalent).

---

## 8. Verify

```bash
# Wait for the service to reach a steady state
aws ecs wait services-stable --cluster ${APP}-cluster --services ${APP}-service --region $AWS_REGION

# Get the public URL again and open it in a browser
aws elbv2 describe-load-balancers --names ${APP}-alb \
  --query 'LoadBalancers[0].DNSName' --output text --region $AWS_REGION
```

Visit `http://<that-DNS-name>/`, upload `sample_customers.csv`, and check
`/data`. If the page 503s for a minute, the task is still starting or failing its
health check — see debugging below.

---

## 9. AWS debugging & monitoring

The AWS equivalents of your local Docker workflow:

### `docker ps` → list running tasks
```bash
aws ecs list-tasks --cluster aws-container-cluster --region us-east-2
aws ecs describe-services --cluster aws-container-cluster \
  --services aws-container-service --region us-east-2 \
  --query 'services[0].{running:runningCount,desired:desiredCount,events:events[:5]}'
```

### `docker logs` → CloudWatch logs
```bash
aws logs tail /ecs/aws-container-task --follow --region us-east-2
```

### `docker exec` → ECS Exec (shell inside a running task)
```bash
# Get a task ARN first:
TASK_ARN=$(aws ecs list-tasks --cluster aws-container-cluster \
  --query 'taskArns[0]' --output text --region us-east-2)

aws ecs execute-command --cluster aws-container-cluster \
  --task $TASK_ARN --container aws-container-web \
  --command "/bin/sh" --interactive --region us-east-2
```
*(Requires the Session Manager plugin for the AWS CLI and `--enable-execute-command` on the service.)*

### `docker inspect` → describe the task/def
```bash
aws ecs describe-tasks --cluster aws-container-cluster --tasks $TASK_ARN --region us-east-2
aws ecs describe-task-definition --task-definition aws-container-task --region us-east-2
```

### Common issues
- **Task keeps restarting / health check failing:** check logs; confirm the app
  reaches RDS (SG rules), and that `DATABASE_URL` is correct.
- **ALB 503:** no healthy targets — task not passing `/health` yet, or task
  crashed on startup.
- **Task stuck in PENDING / image pull error:** execution role missing
  `AmazonECSTaskExecutionRolePolicy`, or `assignPublicIp` disabled without a NAT.
- **Cannot connect to DB:** RDS SG must allow 5432 from the **task SG**.

---

## 🎓 Interview preparation

How this project maps to the concepts interviewers love to probe.

### Observability
- **Logs:** app logs go to **stdout** → captured by the `awslogs` driver → viewable
  in **CloudWatch Logs** (`aws logs tail`). Structured/JSON logs make querying easier.
- **Metrics:** ECS/Fargate publish **CPU & memory utilization** to CloudWatch; the
  ALB publishes **request count, latency, 4xx/5xx, healthy host count**.
- **Health checks:** the `/health` endpoint gives both Docker and the ALB a
  binary liveness signal.
- **The three pillars:** logs, metrics, traces. Add **CloudWatch Dashboards** and
  **Alarms** (e.g. alarm on 5xx rate or p95 latency) to close the loop.

### Scalability
- **Horizontal scaling:** increase the ECS service `desired-count`; the ALB spreads
  traffic. Add **Application Auto Scaling** with a **target-tracking policy**
  (e.g. keep average CPU at 60%) so tasks scale in/out automatically.
- **Vertical scaling:** raise task `cpu`/`memory`.
- **Stateless app:** because all state is in RDS, any task can serve any request —
  the prerequisite for horizontal scaling.
- **DB scaling:** read replicas for read-heavy loads; larger instance class or
  Aurora for more throughput.

### Latency optimization
- Run tasks in **multiple AZs** near users; use the ALB to route to the closest
  healthy task.
- **Connection pooling** (`pool_pre_ping`, `pool_recycle` here) avoids reconnect
  overhead; consider **RDS Proxy** to pool DB connections at scale.
- **gunicorn workers/threads** tuned to CPU; cache hot reads (e.g. ElastiCache/Redis).
- Keep images small (multi-stage) for **faster cold starts** during scale-out.

### Distributed tracing
- Instrument with **AWS X-Ray** (or **OpenTelemetry** → X-Ray) to trace a request
  across ALB → app → RDS and see where time is spent.
- Propagate a **trace/correlation ID** header through logs so a single request can
  be followed across services.
- The **X-Ray sidecar/daemon** can run as an additional container in the task def.

### High availability
- **≥2 tasks across ≥2 AZs** behind the ALB; if one AZ/task fails, others serve.
- **RDS Multi-AZ** provides an automatic standby failover (enable for production).
- ECS **service scheduler** automatically replaces failed/unhealthy tasks.
- **Health checks + deregistration delay** enable safe rolling deploys with zero
  downtime.

### Cost optimization
- **Fargate** = pay only for the vCPU/RAM you request while tasks run — right-size
  them. Use **Fargate Spot** for non-critical/burst workloads (up to ~70% cheaper).
- Turn things off when learning (**TEARDOWN.md**); RDS + ALB bill hourly even when idle.
- Set **CloudWatch Logs retention** (e.g. 7 days) so log storage doesn't grow forever.
- Avoid a **NAT gateway** for this demo by using public subnets + `assignPublicIp`.
- Use **auto scaling** to scale to a low baseline off-peak; **Savings Plans** for
  steady long-term usage.

### Resource monitoring
- **CloudWatch metrics** for ECS (CPUUtilization, MemoryUtilization) and ALB
  (TargetResponseTime, RequestCount, HTTPCode_Target_5XX_Count, HealthyHostCount).
- **Container Insights** (enable on the cluster) gives per-task/per-container
  dashboards.
- **CloudWatch Alarms** → SNS notifications (email/Slack) when thresholds breach.
- **RDS metrics:** CPU, freeable memory, connections, read/write IOPS, free storage;
  enable **Performance Insights** to find slow queries.
```
