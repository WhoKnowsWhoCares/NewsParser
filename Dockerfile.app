FROM python:3.10-slim as base

# set environment variables
ENV PYTHONDONTWRITEBYTECODE 1 \
    PYTHONFAULTHANDLER=1 \
    PYTHONHASHSEED=random \
    PYTHONUNBUFFERED=1

# Set the working directory to /app
RUN mkdir /app
WORKDIR /app

COPY requirements.txt .

RUN --mount=type=cache,target=/root/.cache/pip \
        pip install --no-cache-dir -r requirements.txt

COPY main.py /app
COPY .env /app
COPY src/ /app/src/
COPY newsparser.session /app
RUN mkdir /app/logs

CMD ["python", "main.py"]
