FROM jrottenberg/ffmpeg:7-ubuntu

RUN apt-get update && \
    apt-get install -y nginx supervisor python3 python3-pip && \
    rm -rf /var/lib/apt/lists/*

RUN pip3 install --break-system-packages flask

COPY . /app
WORKDIR /app

RUN mkdir -p /app/output /var/log/ffmpeg

RUN rm -rf /etc/nginx/sites-enabled/*
COPY nginx.conf /etc/nginx/sites-available/default
RUN ln -s /etc/nginx/sites-available/default /etc/nginx/sites-enabled/

COPY supervisord.conf /etc/supervisor/conf.d/supervisord.conf

EXPOSE 80

CMD ["/usr/bin/supervisord", "-c", "/etc/supervisor/conf.d/supervisord.conf"]
