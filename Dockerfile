FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY web_app.py conciliador.py db.py wsgi.py ./
COPY templates/ templates/
COPY static/ static/

# Datos de runtime (uploads/, users.json, Empresas.xlsx, DATOS BANCARIOS...xlsx) no
# se copian a la imagen: se montan como volúmenes en docker-compose.yml.
RUN mkdir -p uploads

EXPOSE 8080

CMD ["python", "wsgi.py"]
