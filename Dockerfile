#1 definición de la imagen base oficial de Python
FROM python:3.12-slim

# 2 variables de entorno para optimizar el rendimiento
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

#3 Directorio de trabajo dentro del contenedor
WORKDIR /app

#4 instalación de dependencias del sistema operativo para PostgreSQL
RUN apt-get update \
    && apt-get install -y --no-install-recommends gcc libpq-dev \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

#5 copiar requerimientos e instalarlos
COPY requerimientos.txt /app/
RUN pip install --upgrade pip
RUN pip install -r requerimientos.txt
COPY . /app/

# recopilar archivos estáticos automaticamente sin pedir confirmacion
RUN python manage.py collectstatic --noinput

#6 Exponer el puerto
EXPOSE 8000

#7 Motor de arranque en produccion: migrar y luego iniciar Gunicorn
CMD python manage.py migrate && gunicorn configuracion.wsgi:application --bind 0.0.0.0:8000