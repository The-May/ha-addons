FROM alpine:3.19
RUN apk add --no-cache curl python3 jq
COPY run.sh /run.sh
RUN chmod +x /run.sh
CMD ["/run.sh"]
