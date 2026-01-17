# 使用精简镜像，镜像体积从 1.2G 下降为约 400M，提高启动效率，同时升级到 Python 3.11.x 提高 20% 以上性能
FROM python:3.13-slim-bullseye

# 升级 pip 到最新版
RUN pip install --upgrade pip

# 设置 build 工作目录
WORKDIR /build

# 1. 先拷贝 requirements.txt 进行依赖安装 (利用 Docker 缓存机制)
COPY requirements.txt .

# 安装依赖 (包含 gunicorn)
# 注意：这里同时安装了 gunicorn，因为它不在 requirements.txt 中
RUN pip install --no-cache-dir -r requirements.txt gunicorn -i http://mirrors.aliyun.com/pypi/simple/ --trusted-host=mirrors.aliyun.com

# 2. 拷贝所有源码
COPY . .

# 3. 安装本地项目 (这会将本地修改后的代码安装到 site-packages)
RUN pip install --no-cache-dir .

# 清理构建目录 (可选)
WORKDIR /
RUN rm -rf /build

# 设置工作目录方便启动 (指向 site-packages 中的安装位置)
ENV APP_HOME=/usr/local/lib/python3.13/site-packages/aktools
WORKDIR $APP_HOME

# 默认启动 gunicorn 服务
# 注意：timeout-keep-alive 默认并不是 gunicorn 的参数，这里传递给 uvicorn 需要通过 --timeout-keep-alive 吗？
# Gunicorn + UvicornWorker 模式下，配置通常通过 gunicorn conf 或者 命令行参数传递。
# 但是 UvicornWorker 接受 --timeout-keep-alive 吗? 
# 通常做法是 gunicorn --keep-alive <seconds>。
# 不过既然我们在 main.py 里都没改 gunicorn 的配置，这里保持原样即可，或者根据用户之前的修改调整。
# 之前的修改是在 main.py 的 uvicorn.run 中。
# 但 Docker 中是用 gunicorn 启动 main:app。
# 这里的 behavior 和 main.py if __name__ == "__main__" 是不一样的。
# 如果想在 Docker (Gunicorn) 中生效，需要在 gunicorn 命令中设置 keep-alive。
# Gunicorn 的默认 keep-alive 是 2 秒。
# 为了匹配用户的 5分钟 (300s) 需求，我们应该在这里也加上 --keep-alive 300
CMD ["gunicorn", "--bind", "0.0.0.0:8080", "main:app", "-k", "uvicorn.workers.UvicornWorker", "--keep-alive", "300"]