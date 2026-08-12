# These two are the entire coupling between this layer and whatever cluster it
# targets. Point them at AKS and this directory works unchanged.
variable "kubeconfig_path" {
  description = "Path to the kubeconfig for the target cluster."
  type        = string
  default     = "~/.kube/config"
}

variable "kube_context" {
  description = "kubectl context to use. For the local kind cluster: kind-cicd-lab."
  type        = string
  default     = "kind-cicd-lab"
}

variable "namespace" {
  description = "Namespace to deploy into. Created here with Pod Security Admission labels."
  type        = string
  default     = "phishing-detector"
}

variable "release_name" {
  description = "Helm release name."
  type        = string
  default     = "phishing-detector"
}

variable "image_tag" {
  description = "Container image tag to deploy."
  type        = string
  default     = "0.1.0"
}

variable "detector_threshold" {
  description = <<-EOT
    Decision threshold. Deliberately different from the 0.5 baked into the image,
    so a running pod reporting this value proves it is reading the ConfigMap and
    not its own image layer.
  EOT
  type        = number
  default     = 0.30

  validation {
    condition     = var.detector_threshold >= 0 && var.detector_threshold <= 1
    error_message = "detector_threshold must be between 0 and 1."
  }
}

variable "create_client_namespace" {
  description = "Create a namespace labelled as an allowed NetworkPolicy client."
  type        = bool
  default     = true
}

variable "client_namespace" {
  description = "Name of the client namespace."
  type        = string
  default     = "detector-clients"
}
