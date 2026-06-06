FROM node:20-alpine
WORKDIR /app
COPY package*.json ./
RUN npm install 2>/dev/null || true
COPY . .
EXPOSE 3000
CMD ["npm", "run", "dev", "--", "--host"]
