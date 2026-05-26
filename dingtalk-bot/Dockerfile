FROM public.ecr.aws/docker/library/python:3.12-slim

# procps provides pgrep, used by the ECS health check
RUN apt-get update \
    && apt-get install -y --no-install-recommends procps \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Inject DevOps Agent service model (DevOps Agent is a new service not yet in standard boto3)
COPY botocore-ext/devops-agent /usr/local/lib/python3.12/site-packages/botocore/data/devops-agent

COPY app.py .

RUN useradd -u 1000 -m botuser
USER 1000

CMD ["python", "app.py"]
