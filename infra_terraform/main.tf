
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
    from_port       = 80
    to_port         = 80
    protocol        = "tcp"
    security_groups = [aws_security_group.alb_sg[0].id]
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
  instance_type          = "t3.medium" # c6a.2xlarge / g4dn.xlarge
  subnet_id              = aws_subnet.public_subnet_01[0].id
  vpc_security_group_ids = [aws_security_group.backend_sg[0].id]
  iam_instance_profile   = aws_iam_instance_profile.ec2_profile.name

  root_block_device {
    volume_size = 30
    volume_type = "gp3"
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
              cd /opt
              npm install -g @anthropic-ai/claude-code              
              curl -fsSL https://ollama.com/install.sh | sh
              curl -LO https://github.com/gitpod-io/openvscode-server/releases/download/openvscode-server-v1.109.5/openvscode-server-v1.109.5-linux-x64.tar.gz
              tar -xzf openvscode-server-*.gz
              cd openvscode-server-v1.109.5-linux-x64
              cd bin
              export PATH="$(pwd):$PATH"
              echo "export PATH=\"$(pwd):\$PATH\"" >> ~/.bashrc
              source ~/.bashrc
              nohup openvscode-server --host 0.0.0.0 --without-connection-token > vscode.log &
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

# =============================================================================
# ALB + CloudFront for Frontend
# =============================================================================

# Security Group for ALB
resource "aws_security_group" "alb_sg" {
  count       = var.create_vpc ? 1 : 0
  name        = "${var.project_tag}-alb-sg"
  description = "Security group for ALB"
  vpc_id      = aws_vpc.demo_main_vpc[0].id

  ingress {
    from_port   = 80
    to_port     = 80
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }

  ingress {
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

# Application Load Balancer
resource "aws_lb" "frontend_alb" {
  count              = var.create_vpc ? 1 : 0
  name               = "${var.project_tag}-frontend-alb"
  internal           = false
  load_balancer_type = "application"
  security_groups    = [aws_security_group.alb_sg[0].id]
  subnets            = aws_subnet.public_subnet_01[*].id

  tags = {
    Name = "${var.project_tag}-frontend-alb"
  }
}

# Target Group for Frontend EC2
resource "aws_lb_target_group" "frontend_tg" {
  count    = var.create_vpc ? 1 : 0
  name     = "${var.project_tag}-frontend-tg"
  port     = 80
  protocol = "HTTP"
  vpc_id   = aws_vpc.demo_main_vpc[0].id

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

# Register Frontend EC2 with Target Group
resource "aws_lb_target_group_attachment" "frontend_tg_attachment" {
  count            = var.create_vpc ? 1 : 0
  target_group_arn = aws_lb_target_group.frontend_tg[0].arn
  target_id        = aws_instance.frontend[0].id
  port             = 80
}

# ALB Listener (HTTP)
resource "aws_lb_listener" "frontend_http" {
  count             = var.create_vpc ? 1 : 0
  load_balancer_arn = aws_lb.frontend_alb[0].arn
  port              = 80
  protocol          = "HTTP"

  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.frontend_tg[0].arn
  }
}

# CloudFront Distribution
resource "aws_cloudfront_distribution" "frontend_cdn" {
  count   = var.create_vpc ? 1 : 0
  enabled = true
  comment = "${var.project_tag} frontend distribution"

  origin {
    domain_name = aws_lb.frontend_alb[0].dns_name
    origin_id   = "alb-frontend"

    custom_origin_config {
      http_port              = 80
      https_port             = 443
      origin_protocol_policy = "http-only"
      origin_ssl_protocols   = ["TLSv1.2"]
    }
  }

  default_cache_behavior {
    allowed_methods        = ["GET", "HEAD", "OPTIONS", "PUT", "POST", "PATCH", "DELETE"]
    cached_methods         = ["GET", "HEAD"]
    target_origin_id       = "alb-frontend"
    viewer_protocol_policy = "redirect-to-https"

    forwarded_values {
      query_string = true
      headers      = ["Origin", "Authorization"]

      cookies {
        forward = "all"
      }
    }

    min_ttl     = 0
    default_ttl = 0
    max_ttl     = 0
  }

  # Cache behavior for /api/* - no caching, pass everything through
  ordered_cache_behavior {
    path_pattern           = "/api/*"
    allowed_methods        = ["GET", "HEAD", "OPTIONS", "PUT", "POST", "PATCH", "DELETE"]
    cached_methods         = ["GET", "HEAD"]
    target_origin_id       = "alb-frontend"
    viewer_protocol_policy = "redirect-to-https"

    forwarded_values {
      query_string = true
      headers      = ["*"]

      cookies {
        forward = "all"
      }
    }

    min_ttl     = 0
    default_ttl = 0
    max_ttl     = 0
  }

  # Cache behavior for static assets
  ordered_cache_behavior {
    path_pattern           = "/assets/*"
    allowed_methods        = ["GET", "HEAD"]
    cached_methods         = ["GET", "HEAD"]
    target_origin_id       = "alb-frontend"
    viewer_protocol_policy = "redirect-to-https"

    forwarded_values {
      query_string = false

      cookies {
        forward = "none"
      }
    }

    min_ttl     = 0
    default_ttl = 86400
    max_ttl     = 31536000
    compress    = true
  }

  restrictions {
    geo_restriction {
      restriction_type = "none"
    }
  }

  viewer_certificate {
    cloudfront_default_certificate = true
  }

  tags = {
    Name = "${var.project_tag}-frontend-cdn"
  }
}

# =============================================================================
# Outputs
# =============================================================================

output "cloudfront_domain_name" {
  description = "CloudFront distribution domain name"
  value       = var.create_vpc ? aws_cloudfront_distribution.frontend_cdn[0].domain_name : null
}

output "alb_dns_name" {
  description = "ALB DNS name"
  value       = var.create_vpc ? aws_lb.frontend_alb[0].dns_name : null
}

output "frontend_instance_public_ip" {
  description = "Frontend EC2 public IP"
  value       = var.create_vpc ? aws_instance.frontend[0].public_ip : null
}

output "backend_instance_public_ip" {
  description = "Backend EC2 public IP"
  value       = var.create_vpc ? aws_instance.backend[0].public_ip : null
}
