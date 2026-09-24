FROM python:3.12-slim-bookworm

# Agent B video remux/compress (Whapi needs H.264 mp4 under ~48MB)
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/* \
    && ffmpeg -version

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY sub_agent_b ./sub_agent_b
COPY start.py ./start.py

ENV PORT=8080
EXPOSE 8080

# Do not use shell $PORT — Railway may pass it literally to uvicorn
CMD ["python", "start.py"]
