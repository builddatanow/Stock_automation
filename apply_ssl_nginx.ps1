# Writes the full HTTPS nginx config after SSL cert is obtained
$conf = "worker_processes 1;`nevents { worker_connections 1024; }`nhttp {`n    include       mime.types;`n    default_type  application/octet-stream;`n    sendfile on;`n    keepalive_timeout 65;`n`n    server {`n        listen 80;`n        server_name builddatanow.com www.builddatanow.com;`n        location /.well-known/acme-challenge/ {`n            root C:/nginx/html;`n        }`n        location / {`n            return 301 https://`$host`$request_uri;`n        }`n    }`n`n    server {`n        listen 443 ssl;`n        server_name builddatanow.com www.builddatanow.com;`n        ssl_certificate     C:/nginx/ssl/builddatanow.com-chain.pem;`n        ssl_certificate_key C:/nginx/ssl/builddatanow.com-key.pem;`n        ssl_protocols       TLSv1.2 TLSv1.3;`n        ssl_ciphers         HIGH:!aNULL:!MD5;`n        location / {`n            proxy_pass         http://127.0.0.1:5000;`n            proxy_http_version 1.1;`n            proxy_set_header   Host              `$host;`n            proxy_set_header   X-Real-IP         `$remote_addr;`n            proxy_set_header   X-Forwarded-For   `$proxy_add_x_forwarded_for;`n            proxy_set_header   X-Forwarded-Proto `$scheme;`n            proxy_read_timeout 120s;`n        }`n    }`n}`n"
[System.IO.File]::WriteAllText("C:\nginx\conf\nginx.conf", $conf, [System.Text.Encoding]::ASCII)
Write-Host "HTTPS config written. Reloading nginx..."
Push-Location "C:\nginx"
& ".\nginx.exe" -s reload
Pop-Location
Write-Host "Done! Site should be live at https://www.builddatanow.com"
