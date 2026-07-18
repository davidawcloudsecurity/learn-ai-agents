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

# ALB Security Group - allows inbound HTTP/HTTPS from anywhere
resource "aws_security_group" "alb_sg" {
  count       = var.create_vpc ? 1 : 0
  name        = "${var.project_tag}-alb-sg"
  description = "Security group for ALB"
  vpc_id      = aws_vpc.main[0].id

  ingress {
    description = "HTTP from anywhere"
    from_port   = 80
    to_port     = 80
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  ingress {
    description = "HTTPS from anywhere"
    from_port   = 443
    to_port     = 443
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

  # Allow HTTP from ALB
  ingress {
    description     = "HTTP from ALB"
    from_port       = 80
    to_port         = 80
    protocol        = "tcp"
    security_groups = [aws_security_group.alb_sg[0].id]
  }

  # Allow app port (8501 - Streamlit) from ALB
  ingress {
    description     = "App port 8501 from ALB"
    from_port       = 8501
    to_port         = 8501
    protocol        = "tcp"
    security_groups = [aws_security_group.alb_sg[0].id]
  }

  # Allow backend port (8000) from ALB
  ingress {
    description     = "Backend port 8000 from ALB"
    from_port       = 8000
    to_port         = 8000
    protocol        = "tcp"
    security_groups = [aws_security_group.alb_sg[0].id]
  }

  # Allow all traffic from within VPC CIDR
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
              apt update
              apt install -y nginx git curl
              curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash -
              apt install -y nodejs
              rm /etc/nginx/sites-enabled/default
              cat > /etc/nginx/sites-available/app <<'NGINX'
              server {
                listen 80;
                root /opt/app/dist;
                index index.html;
                location /api/ {
                  proxy_pass http://localhost:8501;
                  proxy_read_timeout 300s;
                  proxy_connect_timeout 75s;
                  proxy_send_timeout 300s;
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
              cd /opt
              git clone --filter=blob:none --sparse https://github.com/davidawcloudsecurity/learn-claude-code-workshops.git app
              cd app
              git sparse-checkout set ship-your-first-managed-agent
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
              apt update -y
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

# Target Group - Frontend (port 8501)
resource "aws_lb_target_group" "frontend_tg" {
  count    = var.create_vpc ? 1 : 0
  name     = "${var.project_tag}-frontend-tg"
  port     = 8501
  protocol = "HTTP"
  vpc_id   = aws_vpc.main[0].id

  health_check {
    enabled             = true
    healthy_threshold   = 3
    unhealthy_threshold = 3
    timeout             = 5
    interval            = 30
    path                = "/"
    matcher             = "200"
  }

  tags = {
    Name = "${var.project_tag}-frontend-tg"
  }
}

# Target Group - Backend (port 8000)
resource "aws_lb_target_group" "backend_tg" {
  count    = var.create_vpc ? 1 : 0
  name     = "${var.project_tag}-backend-tg"
  port     = 8000
  protocol = "HTTP"
  vpc_id   = aws_vpc.main[0].id

  health_check {
    enabled             = true
    healthy_threshold   = 3
    unhealthy_threshold = 3
    timeout             = 5
    interval            = 30
    path                = "/"
    matcher             = "200"
  }

  tags = {
    Name = "${var.project_tag}-backend-tg"
  }
}

# Register Frontend EC2 with Target Group
resource "aws_lb_target_group_attachment" "frontend" {
  count            = var.create_vpc ? 1 : 0
  target_group_arn = aws_lb_target_group.frontend_tg[0].arn
  target_id        = aws_instance.frontend[0].id
  port             = 8501
}

# Register Backend EC2 with Target Group
resource "aws_lb_target_group_attachment" "backend" {
  count            = var.create_vpc ? 1 : 0
  target_group_arn = aws_lb_target_group.backend_tg[0].arn
  target_id        = aws_instance.backend[0].id
  port             = 8000
}

# ALB Listener - HTTP (port 80) → Frontend by default
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

# ALB Listener Rule - /api/* routes to backend
resource "aws_lb_listener_rule" "backend_api" {
  count        = var.create_vpc ? 1 : 0
  listener_arn = aws_lb_listener.http[0].arn
  priority     = 100

  action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.backend_tg[0].arn
  }

  condition {
    path_pattern {
      values = ["/api/*"]
    }
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
