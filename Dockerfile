# K8-Optimizer operator/API image.
FROM python:3.12-slim

WORKDIR /app

COPY k8optimizer/ k8optimizer/
COPY examples/ examples/

EXPOSE 8080

# The engine is pure stdlib; no pip install needed.
CMD ["python", "-m", "k8optimizer.cli", "--serve", "--port", "8080"]