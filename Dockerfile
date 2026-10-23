FROM python:3.12-slim
WORKDIR /srv
COPY requirements.txt ./
RUN pip install --no-cache-dir --require-hashes -r requirements.txt
COPY app ./app
RUN useradd --uid 10001 --create-home app && mkdir /srv/data && chown -R app:app /srv/app /srv/data
USER app
EXPOSE 8000
CMD ["uvicorn", "app.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--no-proxy-headers"]
