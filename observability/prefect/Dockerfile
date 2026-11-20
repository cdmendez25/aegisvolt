FROM prefecthq/prefect:3-latest
RUN pip install --no-cache-dir boto3 httpx
WORKDIR /opt/flows
COPY flows.py .
