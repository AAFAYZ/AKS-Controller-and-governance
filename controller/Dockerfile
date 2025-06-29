ARG PYTHON_BASE_IMAGE_VERSION=3.13-slim
FROM <acrname>.azurecr.io/python:${PYTHON_BASE_IMAGE_VERSION}
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
# Can be overridden in build args if needed
ARG UID=1001
ARG GID=1001
# Create a non-root user and group with specific UID and GID
RUN groupadd --system --gid ${GID} kvp && useradd --system --create-home --uid ${UID} --gid ${GID} --shell /bin/bash kvpuser
WORKDIR /app
COPY . /app
RUN chown -R kvpuser:kvp /app
USER kvpuser
RUN pip install --no-cache-dir -r requirements.txt
ENTRYPOINT ["python", "controller.py"]
