from django.contrib import admin
from django.apps import apps

app_config = apps.get_app_config('nucleo_sistema')

for model_name, model in app_config.models.items():
    try:
        admin.site.register(model)
    except admin.sites.AlreadyRegistered:
        pass
    except Exception as e:
        print(f"Error al registrar {model_name}: {e}")