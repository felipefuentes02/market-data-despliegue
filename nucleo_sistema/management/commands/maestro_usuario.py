from django.core.management.base import BaseCommand
from nucleo_sistema.models import Usuario
from django.contrib.auth.hashers import make_password
from django.utils import timezone

class Command(BaseCommand):
    def handle(self, *args, **options):
        #configuracion usuario maestro
        usuarios_a_crear = [
    {
        "username": "master_marketdata",
        "nombre": "ADMINISTRADOR",
        "apellido1": "CORPORATIVO",
        "rol": "CORPORATIVO",
        "mail": "soporte@marketdata.cl",
        "pass": "master123",
        "tienda": None # Este usuario no está asociado a una tienda específica
    }
]
        for u in usuarios_a_crear:
            #aquuí no se crea el usuario si ya existe, para evitar errores de integridad y duplicados
            if not Usuario.objects.filter(nombre_usuario=u['username']).exists():
                Usuario.objects.create(
                    nombre_usuario=u['username'],
                    nombre=u['nombre'],
                    primer_apellido=u['apellido1'],
                    segundo_apellido="EMPRESA",
                    rol=u['rol'],
                    mail=u['mail'],
                    password=make_password(u['pass']),
                    es_activo=True,
                    requiere_cambio_pass=False,
                    fecha_creacion=timezone.now(),
                    rut_tienda_id=u['tienda']
                )
                self.stdout.write(self.style.SUCCESS(f'✅ Usuario {u["username"]} creado exitosamente.'))
            else:
                self.stdout.write(self.style.WARNING(f'⚠️ El usuario {u["username"]} ya existe.'))