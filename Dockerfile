FROM python:3.12-slim

WORKDIR /app
COPY . /app

CMD ["python3", "-m", "unittest", "discover", "-s", "tests", "-p", "test_*.py", "-v"]
