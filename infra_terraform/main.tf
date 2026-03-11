
# Define AWS as the provider with the specified region.
provider "aws" {
  region = "us-east-1"
}

# Create an AWS VPC with the specified CIDR block and tags.
resource "aws_vpc" "demo_main_vpc" {
  count                = var.create_vpc ? 1 : 0
  cidr_block           = var.main_cidr_block
  enable_dns_hostnames = true
  enable_dns_support   = true
  tags = {
    Name = var.project_tag
  }
}

# Internet Gateway
resource "aws_internet_gateway" "demo_igw" {
  count  = var.create_vpc ? 1 : 0
  vpc_id = var.create_vpc ? aws_vpc.demo_main_vpc[0].id : null
  tags = {
    Name = "${var.project_tag}-igw"
  }
}

# Data source for existing VPC (when not creating new one)
data "aws_vpc" "existing" {
  count = var.create_vpc ? 0 : 1
  
  filter {
    name   = "tag:Name"
    values = [var.project_tag]
  }
}

resource "aws_subnet" "public_subnet_01" {
  count                   = var.create_vpc ? length(var.public_subnet_cidrs) : 0
  vpc_id                  = var.create_vpc ? aws_vpc.demo_main_vpc[0].id : null
  cidr_block              = var.public_subnet_cidrs[count.index]
  availability_zone       = var.azs[count.index]
  map_public_ip_on_launch = true
  tags = {
    Name = "${var.project_tag}-pb-sub-01"
  }
}

# Data source for existing public subnets
data "aws_subnets" "existing_public" {
  count = var.create_vpc ? 0 : 1
  
  filter {
    name   = "vpc-id"
    values = [data.aws_vpc.existing[0].id]
  }
  
  filter {
    name   = "tag:Name"
    values = ["${var.project_tag}-pb-sub-01"]
  }
}

resource "aws_subnet" "private_subnet_01" {
  count             = var.create_vpc ? length(var.private_subnet_cidrs) : 0
  vpc_id            = var.create_vpc ? aws_vpc.demo_main_vpc[0].id : null
  cidr_block        = var.private_subnet_cidrs[count.index]
  availability_zone = var.azs[count.index]
  tags = {
    Name = "${var.project_tag}-pv-sub-01"
  }
}

# Public Route Table
resource "aws_route_table" "public_rt" {
  count  = var.create_vpc ? 1 : 0
  vpc_id = var.create_vpc ? aws_vpc.demo_main_vpc[0].id : null
  
  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = var.create_vpc ? aws_internet_gateway.demo_igw[0].id : null
  }
  
  tags = {
    Name = "${var.project_tag}-public-rt"
  }
}

# Associate public subnets with public route table
resource "aws_route_table_association" "public_rta" {
  count          = var.create_vpc ? length(aws_subnet.public_subnet_01) : 0
  subnet_id      = aws_subnet.public_subnet_01[count.index].id
  route_table_id = aws_route_table.public_rt[0].id
}

# Security Group for Frontend
resource "aws_security_group" "frontend_sg" {
  count       = var.create_vpc ? 1 : 0
  name        = "${var.project_tag}-frontend-sg"
  description = "Security group for frontend EC2"
  vpc_id      = aws_vpc.demo_main_vpc[0].id

  ingress {
    from_port   = 80
    to_port     = 80
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  ingress {
    from_port   = 22
    to_port     = 22
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = {
    Name = "${var.project_tag}-frontend-sg"
  }
}

# Security Group for Backend
resource "aws_security_group" "backend_sg" {
  count       = var.create_vpc ? 1 : 0
  name        = "${var.project_tag}-backend-sg"
  description = "Security group for backend EC2"
  vpc_id      = aws_vpc.demo_main_vpc[0].id

  ingress {
    from_port   = 8000
    to_port     = 8000
    protocol    = "tcp"
    cidr_blocks = ["172.16.0.0/16"]
  }

  ingress {
    from_port   = 22
    to_port     = 22
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = {
    Name = "${var.project_tag}-backend-sg"
  }
}

# IAM Role for EC2 instances
resource "aws_iam_role" "ec2_role" {
  name = "${var.project_tag}-ec2-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Action = "sts:AssumeRole"
      Effect = "Allow"
      Principal = {
        Service = "ec2.amazonaws.com"
      }
    }]
  })
}

resource "aws_iam_instance_profile" "ec2_profile" {
  name = "${var.project_tag}-ec2-profile"
  role = aws_iam_role.ec2_role.name
}

resource "aws_iam_role_policy_attachment" "ssm_policy" {
  role       = aws_iam_role.ec2_role.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

# Get latest Ubuntu AMI
data "aws_ami" "ubuntu" {
  most_recent = true
  owners      = ["099720109477"]

  filter {
    name   = "name"
    values = ["ubuntu/images/hvm-ssd/ubuntu-jammy-22.04-amd64-server-*"]
  }
}

# Backend EC2 Instance
resource "aws_instance" "backend" {
  count                  = var.create_vpc ? 1 : 0
  ami                    = data.aws_ami.ubuntu.id
  instance_type          = "t3.medium"
  subnet_id              = aws_subnet.public_subnet_01[0].id
  vpc_security_group_ids = [aws_security_group.backend_sg[0].id]
  iam_instance_profile   = aws_iam_instance_profile.ec2_profile.name

  user_data = <<-EOF
              #!/bin/bash
              apt update
              apt install -y git curl
              curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash -
              apt install -y nodejs
              cd /opt
              git clone https://github.com/davidawcloudsecurity/learn-lovable-llm.git app
              cd app
              npm install
              npm install -g pm2
              EOF

  tags = {
    Name = "${var.project_tag}-backend"
  }
}

# Frontend EC2 Instance
resource "aws_instance" "frontend" {
  count                  = var.create_vpc ? 1 : 0
  ami                    = data.aws_ami.ubuntu.id
  instance_type          = "t3.small"
  subnet_id              = aws_subnet.public_subnet_01[0].id
  vpc_security_group_ids = [aws_security_group.frontend_sg[0].id]
  iam_instance_profile   = aws_iam_instance_profile.ec2_profile.name

  root_block_device {
    volume_size           = 30
    volume_type           = "gp3"
    delete_on_termination = true
    encrypted             = true
  }

  user_data = <<-EOF
              #!/bin/bash
              apt update
              apt install -y nginx git curl
              curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash -
              apt install -y nodejs
              cd /opt
              git clone https://github.com/davidawcloudsecurity/learn-lovable-llm.git app
              cd app
              npm install
              npm run build
              rm /etc/nginx/sites-enabled/default
              cat > /etc/nginx/sites-available/app <<'NGINX'
              server {
                listen 80;
                root /opt/app/dist;
                index index.html;
                location /api/ {
                  proxy_pass http://localhost:8000;
                  # Increase timeouts for slow LLM responses
                  proxy_read_timeout 300s;      # 5 minutes
                  proxy_connect_timeout 75s;
                  proxy_send_timeout 300s;
                  
                  # Important for streaming
                  proxy_buffering off;
                  proxy_cache off;
                }
                location / {
                  try_files $uri /index.html;
                }
              }
              NGINX
              ln -s /etc/nginx/sites-available/app /etc/nginx/sites-enabled/
              systemctl restart nginx
              curl -fsSL https://ollama.com/install.sh | sh
              ollama run smollm:1.7b
              EOF

  tags = {
    Name = "${var.project_tag}-frontend"
  }
}
