variable "cluster_name" {
  description = "Name of the kind cluster. The kubectl context becomes kind-<name>."
  type        = string
  default     = "cicd-lab"
}

variable "node_image" {
  description = <<-EOT
    kind node image, pinned by digest rather than tag. A tag can be repointed at
    new content; a digest is the content. This is the same discipline applied to
    the Python dependencies — a cluster that comes up as a different Kubernetes
    version next month is not a reproducible environment.
  EOT
  type        = string
  default     = "kindest/node:v1.36.1@sha256:3489c7674813ba5d8b1a9977baea8a6e553784dab7b84759d1014dbd78f7ebd5"
}

variable "pod_subnet" {
  description = "Pod CIDR. Must match CALICO_IPV4POOL_CIDR in the Calico install."
  type        = string
  default     = "100.64.0.0/16"
}

variable "calico_version" {
  description = "Calico release to install as the CNI."
  type        = string
  default     = "v3.32.1"
}

variable "kubeconfig_path" {
  description = "Where kind writes the kubeconfig."
  type        = string
  default     = "~/.kube/config"
}
