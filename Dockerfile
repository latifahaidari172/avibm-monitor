FROM python:3.11-slim

RUN apt-get update && apt-get install -y \
    wget curl unzip gnupg ca-certificates xvfb xauth \
    libglib2.0-0 libnss3 libfontconfig1 libxrender1 libxss1 \
    libxtst6 libx11-xcb1 libxcomposite1 libxcursor1 libxdamage1 \
    libxi6 libxrandr2 libasound2 libpangocairo-1.0-0 \
    libatk1.0-0 libatk-bridge2.0-0 libgtk-3-0 libdrm2 libgbm1 \
    --no-install-recommends && rm -rf /var/lib/apt/lists/*

# Install Google Chrome
RUN wget -q -O /tmp/google-chrome.deb https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb && \
    apt-get update && \
    apt-get install -y /tmp/google-chrome.deb --no-install-recommends && \
    rm /tmp/google-chrome.deb && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
COPY start.sh /start.sh
RUN chmod +x /start.sh

EXPOSE 8080

CMD ["/start.sh"]
