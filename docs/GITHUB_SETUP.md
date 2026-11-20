# Puesta en marcha en GitHub (último paso)

El código, la infraestructura y los 3 ambientes ya funcionan en local. Esta guía conecta el
repositorio con GitHub para activar la gobernanza: ramas protegidas, PRs, checks y deploy.

## 1. Identidad de git y primer commit

```bash
git config user.name  "Tu Nombre"
git config user.email "tu-correo@ejemplo.com"
git commit -m "feat: plataforma AegisVolt (IaC, microservicios, SAGA, canary, observabilidad)"
git branch test && git branch prod          # las 3 ramas parten del mismo commit
```

## 2. Crear el repositorio (público: los rulesets son gratis en repos públicos)

```bash
gh auth login
gh repo create aegisvolt --public --source . --remote origin
git push -u origin dev test prod
gh repo edit --default-branch dev
```

## 3. Runner self-hosted (ejecuta `terraform-plan` y `deploy` en tu PC, junto a Floci y kind)

En GitHub: *Settings → Actions → Runners → New self-hosted runner → Linux*. Copia los comandos.
En el paso `./config.sh` agrega la etiqueta **`aegisvolt`**:

```bash
./config.sh --url https://github.com/<usuario>/aegisvolt --token <TOKEN> --labels aegisvolt --unattended
./run.sh        # déjalo corriendo en una terminal durante la sustentación
```

> Seguridad: en un repo público, cualquiera podría abrir un PR que ejecute código en tu runner.
> Activa *Settings → Actions → General → "Require approval for all outside collaborators"*.

## 4. Proteger las ramas

```bash
./scripts/github/protect-branches.sh <usuario>/aegisvolt
```

Esto crea el ruleset `aegisvolt-ambientes-protegidos` para dev, test y prod: sin force push,
sin push directo, PR obligatorio, los 6 checks requeridos y sin borrado. No tiene bypass.

Opcional: en *Settings → Environments* crea `prod` con *Required reviewers* para que el deploy
a producción espere una aprobación manual.

## 5. Primer flujo de promoción

```bash
git checkout dev && git pull
git checkout -b feature/primera-funcionalidad
# ... un cambio pequeño, por ejemplo en el README ...
git commit -am "feat: primera funcionalidad" && git push -u origin feature/primera-funcionalidad
gh pr create --base dev --title "feat: primera funcionalidad" --body "Cambio de prueba"
gh pr checks --watch        # 6 checks en verde
gh pr merge --merge         # => deploy.yml despliega dev

gh pr create --base test --head dev  --title "Promote dev -> test"  && gh pr merge --merge
gh pr create --base prod --head test --title "Promote test -> prod" && gh pr merge --merge
```

## 6. Probar las reglas (pregunta de la sustentación)

```bash
git checkout test && git commit --allow-empty -m "push directo" && git push origin test   # RECHAZADO
git push --force origin test                                                              # RECHAZADO
git reset --hard origin/test
```
