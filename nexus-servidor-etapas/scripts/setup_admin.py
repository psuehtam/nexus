"""Run: python -m scripts.setup_admin --username seu.usuario [--reset]."""
import argparse
import getpass
from src import auth


def main():
    parser = argparse.ArgumentParser(description='Criar ou recuperar a conta administrativa NEXUS.')
    parser.add_argument('--username', required=True)
    parser.add_argument('--reset', action='store_true', help='Trocar senha e invalidar todas as sessões do usuário.')
    args = parser.parse_args()
    try:
        args.username = auth.normalize_username(args.username)
    except ValueError as exc:
        parser.error(str(exc))
    print('Use uma senha com pelo menos 12 caracteres. Pode ser uma frase com espaços.')
    print('A senha não aparece enquanto você digita. Ctrl+C cancela esta etapa.')
    try:
        while True:
            password = getpass.getpass('Senha: ')
            try:
                auth.validate_password(password)
            except ValueError as exc:
                print(f'{exc} Tente novamente.')
                continue
            confirmation = getpass.getpass('Repita a senha: ')
            if password != confirmation:
                print('As senhas não conferem. Tente novamente.')
                continue
            break
    except (KeyboardInterrupt, EOFError):
        print('\nCriação cancelada. Execute iniciar.bat novamente para configurar a conta.')
        raise SystemExit(130)
    try:
        auth.create_admin(args.username, password, reset=args.reset)
    except ValueError as exc:
        parser.error(str(exc))
    print('Conta pronta. A senha não foi gravada em texto nem exibida em logs.')


if __name__ == '__main__':
    main()
