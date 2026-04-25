FROM python:3.12-alpine

WORKDIR /app
ENV LANG=C.UTF-8

RUN apk add --no-cache \
    bash \
    gcc \
    musl-dev \
    mariadb-connector-c-dev \
    freetype-dev \
    libpng-dev

RUN pip install --no-cache-dir \
    pandas \
    matplotlib \
    mysqlclient

COPY run.sh /run.sh
COPY plotter.py /app/plotter.py

RUN chmod +x /run.sh

CMD ["/run.sh"]