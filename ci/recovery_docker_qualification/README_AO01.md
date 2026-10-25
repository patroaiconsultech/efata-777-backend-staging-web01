# EFATÀ 777 — qualificação Docker isolada no GitHub Actions

Somente CI, sem Railway, sem autenticação externa, sem migration ou deploy.

Código de referência (Alembic + CI): `b4165bb85762d8fd5a414eb846ec13a4cc97ad1b`.
Quatro arquivos candidatos são copiados para uma árvore temporária do commit exato.
O workflow GitHub Actions preexistente `03-postgres-integration.yml` adiciona
um job Docker na branch `fix/schema-recovery-staging`; não efetua push/deploy.

Execute via Actions, na branch `fix/schema-recovery-staging`, e examine:
- `evidence/summary.txt` (SHA Git, SHA árvore, hashes, status)
- `evidence/docker-build.log` (imagem Docker)
- `evidence/real-module-tests.log` (17 testes, módulo real)
- `evidence/negative-smoke.log` (falha segura sem contexto Railway)

O build não foi executado ao elaborar o pacote; o GitHub Actions será a prova.
Se falhar, não contornar segurança nem executar migrações operacionais.

**HOLD:** publicação do runner Railway, `inspect`, `apply`, troca do backend e produção.
**Rollback CI:** restaurar o YAML anterior e retirar este diretório, após revisão.
