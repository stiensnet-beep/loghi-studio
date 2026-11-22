# Loghi-studio in server mode: looking and searching behind a login. Only the Python standard library is needed.
FROM python:3.14-slim
RUN useradd --uid 10001 --create-home loghi
WORKDIR /app
COPY server.py auth.py names.py books.py readqueue.py corrections.py reading_errors.json index.html login.html ./
USER loghi
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PYTHONIOENCODING=utf-8 \
    LOGHI_MODE=server LOGHI_STUDIO_SETTINGS=/settings LOGHI_STUDIO_CACHE=/cache
EXPOSE 8771
CMD ["python", "server.py", "8771"]
