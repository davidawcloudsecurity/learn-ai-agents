# =============================================================================
# Provider
# =============================================================================

provider "aws" {
  region = "us-east-1"
}

# =============================================================================
# VPC + Networking (Public only, no NAT)
# =============================================================================

resource "aws_vpc" "main" {
  count                = var.create_vpc ? 1 : 0
  cidr_block           = var.main_cidr_block
  enable_dns_hostnames = true
  enable_dns_support   = true

  tags = {
    Name = "${var.project_tag}-vpc"
  }
}

resource "aws_internet_gateway" "igw" {
  count  = var.create_vpc ? 1 : 0
  vpc_id = aws_vpc.main[0].id

  tags = {
    Name = "${var.project_tag}-igw"
  }
}

resource "aws_subnet" "public" {
  count                   = var.create_vpc ? length(var.public_subnet_cidrs) : 0
  vpc_id                  = aws_vpc.main[0].id
  cidr_block              = var.public_subnet_cidrs[count.index]
  availability_zone       = var.azs[count.index]
  map_public_ip_on_launch = true

  tags = {
    Name = "${var.project_tag}-public-subnet-${count.index + 1}"
  }
}

resource "aws_route_table" "public" {
  count  = var.create_vpc ? 1 : 0
  vpc_id = aws_vpc.main[0].id

  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.igw[0].id
  }

  tags = {
    Name = "${var.project_tag}-public-rt"
  }
}

resource "aws_route_table_association" "public" {
  count          = var.create_vpc ? length(var.public_subnet_cidrs) : 0
  subnet_id      = aws_subnet.public[count.index].id
  route_table_id = aws_route_table.public[0].id
}

# =============================================================================
# Security Groups
# =============================================================================

# ALB Security Group - allows inbound HTTP/HTTPS from VPC CIDR
resource "aws_security_group" "alb_sg" {
  count       = var.create_vpc ? 1 : 0
  name        = "${var.project_tag}-alb-sg"
  description = "Security group for ALB"
  vpc_id      = aws_vpc.main[0].id

  ingress {
    description = "HTTP from VPC CIDR"
    from_port   = 80
    to_port     = 80
    protocol    = "tcp"
    cidr_blocks = [var.main_cidr_block]
  }

  ingress {
    description = "HTTPS from VPC CIDR"
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = [var.main_cidr_block]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = {
    Name = "${var.project_tag}-alb-sg"
  }
}

# Shared Security Group for Frontend + Backend EC2
# Allows traffic from ALB SG and from within VPC CIDR
resource "aws_security_group" "ec2_sg" {
  count       = var.create_vpc ? 1 : 0
  name        = "${var.project_tag}-ec2-sg"
  description = "Shared security group for frontend and backend EC2 instances"
  vpc_id      = aws_vpc.main[0].id

  # Allow VS Code Server port (3000) from ALB
  ingress {
    description     = "VS Code Server port 3000 from ALB"
    from_port       = 3000
    to_port         = 3000
    protocol        = "tcp"
    security_groups = [aws_security_group.alb_sg[0].id]
  }

  # Allow all traffic from within VPC CIDR (frontend → backend Ollama on 11434)
  ingress {
    description = "All traffic from VPC CIDR"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = [var.main_cidr_block]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = {
    Name = "${var.project_tag}-ec2-sg"
  }
}

# =============================================================================
# IAM Role for EC2 (SSM + Bedrock)
# =============================================================================

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

resource "aws_iam_role_policy" "bedrock_invoke" {
  name = "${var.project_tag}-bedrock-invoke"
  role = aws_iam_role.ec2_role.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Action = [
        "bedrock:InvokeModel",
        "bedrock:InvokeModelWithResponseStream"
      ]
      Resource = [
        "arn:aws:bedrock:us-east-1::foundation-model/anthropic.claude-sonnet-4-20250514-v1:0"
      ]
    }]
  })
}

# =============================================================================
# AMI
# =============================================================================

data "aws_ami" "ubuntu" {
  most_recent = true
  owners      = ["099720109477"]

  filter {
    name   = "name"
    values = ["ubuntu/images/hvm-ssd/ubuntu-jammy-22.04-amd64-server-*"]
  }
}

# =============================================================================
# EC2 Instances (both in public subnets, same SG)
# =============================================================================

# Frontend EC2 Instance
resource "aws_instance" "frontend" {
  count                  = var.create_vpc ? 1 : 0
  ami                    = data.aws_ami.ubuntu.id
  instance_type          = "t3.medium"
  subnet_id              = aws_subnet.public[0].id
  vpc_security_group_ids = [aws_security_group.ec2_sg[0].id]
  iam_instance_profile   = aws_iam_instance_profile.ec2_profile.name

  root_block_device {
    volume_size           = 30
    volume_type           = "gp3"
    delete_on_termination = true
    encrypted             = true
  }

  user_data = <<-EOF
              #!/bin/bash
              set -e

              # --- System packages ---
              apt update -y
              apt install -y git curl

              # --- Node.js 20 ---
              curl -fsSL https://deb.nodesource.com/setup_20.x | bash -
              apt install -y nodejs
              npm install -g pm2

              # --- Clone application ---
              cd /opt
              git clone https://github.com/davidawcloudsecurity/learn-lovable-llm.git app
              cd /opt/app
              npm install

              # --- OpenVSCode Server (serves directly on port 3000) ---
              cd /opt
              curl -LO https://github.com/gitpod-io/openvscode-server/releases/download/openvscode-server-v1.109.5/openvscode-server-v1.109.5-linux-x64.tar.gz
              tar -xzf openvscode-server-v1.109.5-linux-x64.tar.gz
              rm -f openvscode-server-v1.109.5-linux-x64.tar.gz

              # Start VS Code Server on port 3000 via pm2
              pm2 start /opt/openvscode-server-v1.109.5-linux-x64/bin/openvscode-server \
                --name vscode-server -- --host 0.0.0.0 --without-connection-token
              pm2 save
              pm2 startup systemd -u root --hp /root
              EOF

  tags = {
    Name = "${var.project_tag}-frontend"
  }
}

# Backend EC2 Instance
resource "aws_instance" "backend" {
  count                  = var.create_vpc ? 1 : 0
  ami                    = data.aws_ami.ubuntu.id
  instance_type          = "t3.medium"
  subnet_id              = aws_subnet.public[1].id
  vpc_security_group_ids = [aws_security_group.ec2_sg[0].id]
  iam_instance_profile   = aws_iam_instance_profile.ec2_profile.name

  root_block_device {
    volume_size           = 30
    volume_type           = "gp3"
    delete_on_termination = true
    encrypted             = true
  }

  user_data = <<-EOF
              #!/bin/bash
              set -e

              # --- Install Ollama ---
              curl -fsSL https://ollama.com/install.sh | sh

              # --- Configure Ollama to listen on all interfaces ---
              mkdir -p /etc/systemd/system/ollama.service.d
              cat > /etc/systemd/system/ollama.service.d/override.conf <<'SYSTEMD'
              [Service]
              Environment="OLLAMA_HOST=0.0.0.0"
              SYSTEMD
              systemctl daemon-reload
              systemctl restart ollama

              # --- Pull the model (non-interactive) ---
              ollama pull smollm:1.7b
              EOF

  tags = {
    Name = "${var.project_tag}-backend"
  }
}

# =============================================================================
# Application Load Balancer
# =============================================================================

resource "aws_lb" "alb" {
  count              = var.create_vpc ? 1 : 0
  name               = "${var.project_tag}-alb"
  internal           = false
  load_balancer_type = "application"
  security_groups    = [aws_security_group.alb_sg[0].id]
  subnets            = aws_subnet.public[*].id

  tags = {
    Name = "${var.project_tag}-alb"
  }
}

# Target Group - Frontend (port 3000 - VS Code Server directly)
resource "aws_lb_target_group" "frontend_tg" {
  count    = var.create_vpc ? 1 : 0
  name     = "${var.project_tag}-frontend-tg"
  port     = 3000
  protocol = "HTTP"
  vpc_id   = aws_vpc.main[0].id

  health_check {
    enabled             = true
    healthy_threshold   = 3
    unhealthy_threshold = 3
    timeout             = 5
    interval            = 30
    path                = "/"
    matcher             = "200,302"
  }

  tags = {
    Name = "${var.project_tag}-frontend-tg"
  }
}

# Register Frontend EC2 with Target Group
resource "aws_lb_target_group_attachment" "frontend" {
  count            = var.create_vpc ? 1 : 0
  target_group_arn = aws_lb_target_group.frontend_tg[0].arn
  target_id        = aws_instance.frontend[0].id
  port             = 3000
}

# ALB Listener - HTTP (port 80) → Frontend VS Code
resource "aws_lb_listener" "http" {
  count             = var.create_vpc ? 1 : 0
  load_balancer_arn = aws_lb.alb[0].arn
  port              = 80
  protocol          = "HTTP"

  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.frontend_tg[0].arn
  }
}

# =============================================================================
# Outputs
# =============================================================================

output "alb_dns_name" {
  description = "ALB DNS name"
  value       = var.create_vpc ? aws_lb.alb[0].dns_name : null
}

output "frontend_public_ip" {
  description = "Frontend EC2 public IP"
  value       = var.create_vpc ? aws_instance.frontend[0].public_ip : null
}

output "backend_public_ip" {
  description = "Backend EC2 public IP"
  value       = var.create_vpc ? aws_instance.backend[0].public_ip : null
}

output "backend_private_ip" {
  description = "Backend EC2 private IP (use for Ollama endpoint: http://<this>:11434)"
  value       = var.create_vpc ? aws_instance.backend[0].private_ip : null
}
