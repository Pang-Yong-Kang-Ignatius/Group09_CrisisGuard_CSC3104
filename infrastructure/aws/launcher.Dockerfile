FROM python:3.11-slim
RUN pip install --no-cache-dir 'boto3>=1.35,<2' 'paramiko>=3.5,<5'
WORKDIR /launcher
COPY launch_lab.py ./launch_lab.py
COPY package_source.py ./package_source.py
ENTRYPOINT ["python", "-u", "launch_lab.py"]
