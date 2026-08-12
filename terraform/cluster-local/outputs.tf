
output "cluster_name" {
  description = "Name of the created cluster."
  value       = kind_cluster.this.name
}

output "kube_context" {
  description = "kubectl context name — pass this to the app layer."
  value       = "kind-${kind_cluster.this.name}"
}

output "kubeconfig_path" {
  description = "Kubeconfig path — pass this to the app layer."
  value       = pathexpand(var.kubeconfig_path)
}

output "endpoint" {
  description = "API server endpoint."
  value       = kind_cluster.this.endpoint
}
