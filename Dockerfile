FROM python:3.11-slim

ENV DEBIAN_FRONTEND=noninteractive \
    ANDROID_SDK_ROOT=/opt/android-sdk \
    JAVA_HOME=/usr/lib/jvm/java-17-openjdk-amd64

# JDK + tools needed by the build pipeline (retry: apt mirrors occasionally flake)
RUN for i in 1 2 3; do \
        if apt-get update && apt-get install -y --no-install-recommends \
            openjdk-17-jre-headless \
            unzip \
            curl; then \
            break; \
        fi; \
        echo "apt attempt $i failed, retrying in 10s..."; sleep 10; \
        if [ "$i" = "3" ]; then echo "apt failed after 3 attempts"; exit 1; fi; \
    done && rm -rf /var/lib/apt/lists/*

# Android cmdline-tools -> install SDK packages via sdkmanager
RUN mkdir -p /opt/android-sdk/cmdline-tools && \
    curl -sSL --retry 3 --retry-delay 5 -o /tmp/cmdtools.zip \
      https://dl.google.com/android/repository/commandlinetools-linux-11076708_latest.zip && \
    unzip -q /tmp/cmdtools.zip -d /opt/android-sdk/cmdline-tools && \
    mv /opt/android-sdk/cmdline-tools/cmdline-tools /opt/android-sdk/cmdline-tools/latest && \
    rm /tmp/cmdtools.zip

RUN yes | /opt/android-sdk/cmdline-tools/latest/bin/sdkmanager --sdk_root=/opt/android-sdk \
        --licenses > /dev/null && \
    /opt/android-sdk/cmdline-tools/latest/bin/sdkmanager --sdk_root=/opt/android-sdk \
        "platform-tools" "platforms;android-34" "build-tools;34.0.0"

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY server.py run.sh ./
COPY template ./template

RUN chmod +x run.sh

EXPOSE 8000
CMD ["./run.sh"]
