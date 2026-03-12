@echo off
echo ============================================
echo  Get SSL certificate for builddatanow.com
echo ============================================
echo.
echo This will request a free Let's Encrypt SSL certificate.
echo Make sure DNS is pointed to this server first (45.40.97.163).
echo.
pause

C:\win-acme\wacs.exe --target manual --host builddatanow.com,www.builddatanow.com --validation filesystem --webroot C:\nginx\html --store pemfiles --pemfilespath C:\nginx\ssl

echo.
echo If successful, now run: apply_ssl_nginx.bat
pause
