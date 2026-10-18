{{- /* gopkg.in discovery is remote and can fail per target. The yaml.v3 source
is known; retain its actual version, detected license and local license bytes. */ -}}
{{- range . -}}
{{- $url := .LicenseURL -}}
{{- if eq .Name "gopkg.in/yaml.v3" -}}
{{- $url = printf "https://github.com/go-yaml/yaml/blob/%s/LICENSE" .Version -}}
{{- end -}}
{{ printf "%s\t%s\t%s\t%s\t%s\n" .Name .Version .LicenseName $url .LicensePath }}
{{- end }}
