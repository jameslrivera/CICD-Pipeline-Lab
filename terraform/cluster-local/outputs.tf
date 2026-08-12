# These outputs describe the cluster in terms ANY cluster can be described in —
# a kubeconfig path and a context name — rather than in kind-specific terms.
#
# The app layer takes the same two values as plain input variables and does not
# read this state at all. Wiring it with a remote_state data source would be
# more "correct" Terraform and would also weld the app layer to kind, which is
# exactly what this split exists to prevent.

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
