
terraform {
  required_version = ">= 1.9.0"

  required_providers {
    kind = {
      source  = "tehcyx/kind"
      version = "~> 0.9"
    }
    null = {
      source  = "hashicorp/null"
      version = "~> 3.2"
    }
  }
}

provider "kind" {}

resource "kind_cluster" "this" {
  name       = var.cluster_name
  node_image = var.node_image

  wait_for_ready = false

  kind_config {
    kind        = "Cluster"
    api_version = "kind.x-k8s.io/v1alpha4"

    networking {
      disable_default_cni = true

      pod_subnet = var.pod_subnet
    }

    node { role = "control-plane" }
    node { role = "worker" }
    node { role = "worker" }
  }
}

resource "null_resource" "calico" {
  depends_on = [kind_cluster.this]

  triggers = {
    cluster_id = kind_cluster.this.id
    pod_subnet = var.pod_subnet
  }

  provisioner "local-exec" {
    command     = "${path.module}/../../scripts/install-calico.sh"
    interpreter = ["/bin/bash", "-c"]

    environment = {
      POD_CIDR        = var.pod_subnet
      CALICO_VERSION  = var.calico_version
      KUBECONFIG      = pathexpand(var.kubeconfig_path)
      KUBECTL_CONTEXT = "kind-${var.cluster_name}"
    }
  }
}
