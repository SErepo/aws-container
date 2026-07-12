# 🧹 AWS Teardown Guide — Delete Everything to Stop Costs

Follow this **in order**. AWS bills the **RDS instance**, the **ALB**, and any
**NAT/data transfer** by the hour whether or not anyone visits your app — so
deleting these is what actually stops the charges.

> Deletion order matters: you must remove things that *depend on* a resource
> before the resource itself. Roughly: **ECS service → tasks → ALB/listeners/target
> group → ECS cluster → RDS → security groups → ECR → CloudWatch logs → IAM roles**.

Set your variables again:

```bash
export AWS_REGION=us-east-2
export APP=aws-container
```

---

## 1. Scale down & delete the ECS service

```bash
# Stop all tasks first
aws ecs update-service --cluster ${APP}-cluster --service ${APP}-service \
  --desired-count 0 --region $AWS_REGION

# Delete the service (force in case tasks linger)
aws ecs delete-service --cluster ${APP}-cluster --service ${APP}-service \
  --force --region $AWS_REGION
```

---

## 2. Deregister task definitions (optional but tidy)

```bash
# List all revisions
aws ecs list-task-definitions --family-prefix ${APP}-task --region $AWS_REGION

# Deregister each ARN it prints:
aws ecs deregister-task-definition --task-definition <family:revision> --region $AWS_REGION
```
*(Deregistered task definitions are free; this is just housekeeping.)*

---

## 3. Delete the ECS cluster

```bash
aws ecs delete-cluster --cluster ${APP}-cluster --region $AWS_REGION
```

---

## 4. Delete the load balancer, listener & target group

```bash
# Get ARNs
export ALB_ARN=$(aws elbv2 describe-load-balancers --names ${APP}-alb \
  --query 'LoadBalancers[0].LoadBalancerArn' --output text --region $AWS_REGION)
export TG_ARN=$(aws elbv2 describe-target-groups --names ${APP}-tg \
  --query 'TargetGroups[0].TargetGroupArn' --output text --region $AWS_REGION)

# Deleting the ALB also removes its listeners
aws elbv2 delete-load-balancer --load-balancer-arn $ALB_ARN --region $AWS_REGION

# Wait a bit, then delete the (now unused) target group
sleep 30
aws elbv2 delete-target-group --target-group-arn $TG_ARN --region $AWS_REGION
```

---

## 5. Delete the RDS database (biggest cost saver)

```bash
aws rds delete-db-instance \
  --db-instance-identifier aws-container-db \
  --skip-final-snapshot \
  --delete-automated-backups \
  --region $AWS_REGION

# Optional: wait until fully gone
aws rds wait db-instance-deleted --db-instance-identifier aws-container-db --region $AWS_REGION
```
> `--skip-final-snapshot` avoids a lingering (billable) snapshot. Keep a snapshot
> instead if you want to restore the data later.

---

## 6. Delete the security groups

Security groups can only be deleted once nothing references them, so do this
**after** the service, ALB, and RDS are gone. Delete in dependency order
(RDS SG → task SG → ALB SG), because each references the previous one.

```bash
export VPC_ID=$(aws ec2 describe-vpcs --filters Name=isDefault,Values=true \
  --query 'Vpcs[0].VpcId' --output text --region $AWS_REGION)

for NAME in ${APP}-rds-sg ${APP}-task-sg ${APP}-alb-sg; do
  SG_ID=$(aws ec2 describe-security-groups \
    --filters Name=group-name,Values=$NAME Name=vpc-id,Values=$VPC_ID \
    --query 'SecurityGroups[0].GroupId' --output text --region $AWS_REGION)
  if [ "$SG_ID" != "None" ]; then
    echo "Deleting $NAME ($SG_ID)"
    aws ec2 delete-security-group --group-id $SG_ID --region $AWS_REGION
  fi
done
```
> If a delete fails with *"dependency violation"*, wait a minute (the ALB/ENIs
> take time to detach) and retry.

---

## 7. Delete the ECR repository & images

```bash
aws ecr delete-repository --repository-name $APP --force --region $AWS_REGION
```
> `--force` removes all images inside. Storage is cheap but tidy anyway.

---

## 8. Delete the CloudWatch log group

```bash
aws logs delete-log-group --log-group-name /ecs/${APP}-task --region $AWS_REGION
```

---

## 9. Delete IAM roles & the deploy user (optional)

Only do this if you won't reuse them. `ecsTaskExecutionRole` is shared by many
projects — **skip it if other ECS apps use it.**

```bash
# Detach managed policies, then delete the roles
aws iam detach-role-policy --role-name ecsTaskRole \
  --policy-arn arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore
aws iam delete-role --role-name ecsTaskRole

aws iam detach-role-policy --role-name ecsTaskExecutionRole \
  --policy-arn arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy
aws iam delete-role --role-name ecsTaskExecutionRole

# Deploy user: delete access keys first, then the user (console is easiest).
aws iam list-access-keys --user-name aws-container-deployer
aws iam delete-access-key --user-name aws-container-deployer --access-key-id <KEY_ID>
aws iam delete-user --user-name aws-container-deployer
```

---

## 10. Final verification — confirm nothing is billing

```bash
# No ECS services / clusters
aws ecs list-clusters --region $AWS_REGION

# No load balancers
aws elbv2 describe-load-balancers --region $AWS_REGION --query 'LoadBalancers[].LoadBalancerName'

# No RDS instances
aws rds describe-db-instances --region $AWS_REGION --query 'DBInstances[].DBInstanceIdentifier'

# No ECR repos
aws ecr describe-repositories --region $AWS_REGION --query 'repositories[].repositoryName'
```

Also open the **AWS Billing → Cost Explorer** and the **Billing Dashboard** after
a day to confirm charges have stopped. Consider setting a **Budget alert**
(Billing → Budgets) so you're emailed if spend exceeds, say, $5.

### ✅ Teardown checklist
- [ ] ECS service deleted (desired-count 0 → delete)
- [ ] ECS cluster deleted
- [ ] ALB + listener + target group deleted
- [ ] **RDS instance deleted** (no final snapshot kept unless intended)
- [ ] Security groups deleted (rds → task → alb)
- [ ] ECR repository deleted
- [ ] CloudWatch log group deleted
- [ ] IAM roles/user removed (if not reused)
- [ ] Billing dashboard shows no ongoing charges
