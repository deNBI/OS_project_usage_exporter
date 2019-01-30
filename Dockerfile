FROM python:3.7-alpine

RUN apk add --no-cache linux-headers musl-dev gcc libffi-dev openssl-dev
COPY . /code
WORKDIR /code
RUN pip install --no-cache pipenv && pipenv install --system --deploy --ignore-pipfile

EXPOSE 8080
CMD ["python", "project_usage_exporter.py"]
