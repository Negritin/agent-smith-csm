#!/usr/bin/env python3
"""
Script para criar o primeiro Admin Master no banco de dados.

Uso:
    cd backend
    python scripts/create_admin.py

Pré-requisito:
    A tabela admin_users deve existir no banco.
    Se você rodou o smith_master_setup.sql, a tabela já foi criada!

O script irá solicitar:
- Email do admin
- Senha (será hasheada com bcrypt)
- Nome do admin
"""

import getpass
import os
import sys
from pathlib import Path

# Adiciona o diretório pai ao path para imports
sys.path.insert(0, str(Path(__file__).parent.parent))

try:
    import bcrypt
    from dotenv import load_dotenv

    from supabase import create_client
except ImportError as e:
    print(f"❌ Dependência não encontrada: {e}")
    print("   Execute: pip install bcrypt supabase python-dotenv")
    sys.exit(1)

# Carrega variáveis de ambiente
load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")  # Service Role Key

if not SUPABASE_URL or not SUPABASE_KEY:
    print("❌ Erro: SUPABASE_URL e SUPABASE_KEY devem estar definidos no .env")
    print("   Certifique-se de que o arquivo .env existe e contém essas variáveis")
    sys.exit(1)


def hash_password(password: str) -> str:
    """Hash da senha usando bcrypt com cost 12 (mesmo padrão do frontend)"""
    salt = bcrypt.gensalt(rounds=12)
    hashed = bcrypt.hashpw(password.encode('utf-8'), salt)
    return hashed.decode('utf-8')


def create_admin(email: str, password: str, name: str) -> dict:
    """Cria um novo admin master no banco de dados"""
    supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

    # Verificar se já existe um admin com este email
    existing = supabase.table("admin_users").select("id").eq("email", email.lower()).execute()

    if existing.data:
        raise ValueError(f"Já existe um admin com o email: {email}")

    # Criar o hash da senha
    password_hash = hash_password(password)

    # Inserir no banco
    result = supabase.table("admin_users").insert({
        "email": email.lower().strip(),
        "password_hash": password_hash,
        "name": name.strip(),
        # O schema fresh (schema_completo_v7.0) usa DEFAULT 'company_admin' e NAO tem
        # fallback implicito para master. O primeiro admin criado por este script E o
        # Master Admin, entao a role precisa ser EXPLICITA — senao o login master
        # rejeita com 401 (o auth exige role == 'master_admin' e company_id nulo).
        "role": "master_admin",
        "company_id": None,
    }).execute()

    return result.data[0] if result.data else None


def main():
    print("\n" + "=" * 50)
    print("🔐 Criação do Primeiro Admin Master")
    print("=" * 50 + "\n")

    # Verificar se já existe algum admin
    supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
    existing_admins = supabase.table("admin_users").select("id, email").execute()

    if existing_admins.data:
        print("⚠️  Já existem admins cadastrados:")
        for admin in existing_admins.data:
            print(f"   - {admin['email']}")
        print()
        confirm = input("Deseja criar outro admin mesmo assim? (s/N): ").strip().lower()
        if confirm != 's':
            print("Operação cancelada.")
            sys.exit(0)
        print()

    # Coletar dados do novo admin
    email = input("📧 Email do Admin: ").strip()
    if not email or "@" not in email:
        print("❌ Email inválido")
        sys.exit(1)

    name = input("👤 Nome do Admin: ").strip()
    if not name:
        print("❌ Nome é obrigatório")
        sys.exit(1)

    password = getpass.getpass("🔑 Senha: ")
    if len(password) < 6:
        print("❌ Senha deve ter pelo menos 6 caracteres")
        sys.exit(1)

    password_confirm = getpass.getpass("🔑 Confirme a Senha: ")
    if password != password_confirm:
        print("❌ As senhas não coincidem")
        sys.exit(1)

    print("\n⏳ Criando admin...")

    try:
        admin = create_admin(email, password, name)
        print("\n" + "=" * 50)
        print("✅ Admin Master criado com sucesso!")
        print("=" * 50)
        print(f"   ID: {admin['id']}")
        print(f"   Email: {admin['email']}")
        print(f"   Nome: {admin['name']}")
        print()
        print("🚀 Agora você pode acessar:")
        print("   http://localhost:3000/admin/login")
        print()
    except ValueError as e:
        print(f"\n❌ Erro: {e}")
        sys.exit(1)
    except Exception as e:
        print(f"\n❌ Erro ao criar admin: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
