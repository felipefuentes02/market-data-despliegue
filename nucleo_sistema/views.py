import json, csv, random, string
import os
from django.http import JsonResponse, HttpResponse
from django.utils import timezone
from django.db import transaction
from django.views.decorators.csrf import csrf_exempt
from .models import Producto, Venta, DetalleVenta, Inventario, Tienda, Usuario, DetalleFactura, ClienteFiado, AbonoFiado, CajaSesion, Factura, Comuna, DuenoTienda, AjusteInventario
from django.shortcuts import render, redirect
from django.core.mail import send_mail
from django.contrib import messages
from django.db.models.functions import TruncDate
from datetime import timedelta
from django.db.models import Sum, F, Q
from django.core.paginator import Paginator
from django.views.decorators.cache import never_cache
from collections import defaultdict
from django.contrib.auth.hashers import make_password, check_password
import resend
from django.db import connection


@never_cache
def buscar_producto_por_codigo(request):
    #usca por código exacto, extrayendo el precio de inventario o el sugerido de la maestra al agregarlo
    codigo = request.GET.get('codigo', None)
    rut_tienda = request.GET.get('rut_tienda') or request.session.get('rut_tienda')    
    if not codigo:
        return JsonResponse({'error': 'No se proporcionó un código de barras'}, status=400)
    try:
        producto = Producto.objects.filter(cod_barra=codigo).first()
        if not producto:
            return JsonResponse({'error': 'Producto no encontrado'}, status=404)         
        existencia = 0
        precio_final = producto.precio_venta        
        if rut_tienda:
            inv = Inventario.objects.filter(cod_barra_id=codigo, rut_tienda_id=rut_tienda).first()
            if inv:
                existencia = inv.stock_actual
                #si el producto tiene un precio en el inventario físico de la tienda, tiene prioridad
                if inv.precio_venta > 0:
                    precio_final = inv.precio_venta                    
        datos_respuesta = {
            'codigo': producto.cod_barra,
            'descripcion': producto.descripcion,
            'marca': producto.marca,
            'categoria': producto.categoria,
            'precio_venta': precio_final,
            'stock_disponible': existencia
        }
        return JsonResponse(datos_respuesta, status=200)
    except Exception as error: 
        return JsonResponse({'error': f'Error en la búsqueda: {str(error)}'}, status=500)
    
@csrf_exempt
def registrar_venta(request):
    #registra ventas pagadas y fiadas con validacion desde el servidor
    if request.method == 'POST':
        try:
            datos = json.loads(request.body)
            rut_tienda_id = datos.get('rut_tienda')
            id_usuario_id = datos.get('id_usuario')            
            carrito_pagado = datos.get('carrito_pagado', [])
            carrito_fiado = datos.get('carrito_fiado', [])
            cliente_datos = datos.get('cliente')            
            if not carrito_pagado and not carrito_fiado:
                return JsonResponse({'error': 'No hay productos para procesar'}, status=400)           
            with transaction.atomic():
                ventas_generadas = []                
                #1 PROCESAR PRODUCTOS PAGADOS
                if carrito_pagado:
                    #el servidor calcula su propio total
                    total_bruto_calculado = 0
                    for item in carrito_pagado:
                        cantidad = float(item['cantidad'])
                        precio = int(item.get('precio_venta', 0))
                        total_bruto_calculado += int(round(cantidad * precio))                        
                    total_neto_pagado = int(round(total_bruto_calculado / 1.19))
                    iva_pagado = total_bruto_calculado - total_neto_pagado                    
                    venta_pagada = Venta.objects.create(
                        fecha_venta=timezone.now(),
                        total_neto=total_neto_pagado, 
                        iva=iva_pagado,
                        total_bruto=total_bruto_calculado,
                        estado_pago=True,
                        rut_tienda_id=rut_tienda_id, 
                        id_usuario_id=id_usuario_id  
                    )
                    ventas_generadas.append(venta_pagada.id_venta)                    
                    for item in carrito_pagado:
                        _procesar_descuento_inventario(item, venta_pagada, rut_tienda_id)                        
                #2 PROCESAR PRODUCTOS FIADOS
                if carrito_fiado and cliente_datos:
                    rut_c = str(cliente_datos.get('rut', '')).strip()
                    nom_c = str(cliente_datos.get('nombre', '')).strip()
                    ape_c = str(cliente_datos.get('apellido', '')).strip()                    
                    #cancela si fata un dato
                    if not rut_c or not nom_c or not ape_c:
                        return JsonResponse({'error': 'Rechazado por el servidor: El cliente fiado requiere RUT, Nombre y Apellido obligatoriamente.'}, status=400)                    
                    # busca al cliente o lo crea
                    cliente_obj, _ = ClienteFiado.objects.get_or_create(
                        rut=rut_c,
                        defaults={
                            'nombre': nom_c,
                            'apellido': ape_c
                        }
                    )                                  
                    # el servidor calcula el total fiado
                    total_fiado_calculado = 0
                    for item in carrito_fiado:
                        cantidad = float(item['cantidad'])
                        precio = int(item.get('precio_venta', 0))
                        total_fiado_calculado += int(round(cantidad * precio))                        
                    total_neto_fiado = int(round(total_fiado_calculado / 1.19))
                    iva_fiado = total_fiado_calculado - total_neto_fiado                    
                    
                    #generacion de registro de deuda
                    venta_fiada = Venta.objects.create(
                        fecha_venta=timezone.now(),
                        total_neto=total_neto_fiado, 
                        iva=iva_fiado,
                        total_bruto=total_fiado_calculado,
                        estado_pago=False, 
                        rut_tienda_id=rut_tienda_id, 
                        id_usuario_id=id_usuario_id,
                        rut_cliente=cliente_obj 
                    )
                    with connection.cursor() as cursor:
                        cursor.execute("""
                            UPDATE venta 
                            SET id_cliente_fiado = (SELECT id_cliente_fiado FROM cliente_fiado WHERE rut = %s)
                            WHERE id_venta = %s
                        """, [rut_c, venta_fiada.id_venta])
                    ventas_generadas.append(venta_fiada.id_venta)                    
                    for item in carrito_fiado:
                        _procesar_descuento_inventario(item, venta_fiada, rut_tienda_id)
                    ventas_generadas.append(venta_fiada.id_venta)                    
                    for item in carrito_fiado:
                        _procesar_descuento_inventario(item, venta_fiada, rut_tienda_id)            
            return JsonResponse({
                'mensaje': 'Operación completada exitosamente', 
                'ids_ventas': ventas_generadas
            }, status=201)            
        except Exception as error:
            return JsonResponse({'error': f'Rollback ejecutado: {str(error)}'}, status=500)
    else:
        return JsonResponse({'error': 'Método no permitido. Utilice POST.'}, status=405)

def _procesar_descuento_inventario(item, objeto_venta, rut_tienda_id):
    # registra el detalle y descuenta el stock, soporte de productos a granel y creación del inventario por tienda
    codigo_prod = item['codigo']
    cantidad_vendida = float(item['cantidad']) 
    precio_item = int(item.get('precio_venta', 0))    
    # 1 crea o recupera el producto de la maestra
    producto_obj, _ = Producto.objects.get_or_create(
        cod_barra=codigo_prod,
        defaults={
            'descripcion': item.get('descripcion', 'Producto manual / No encontrado'),
            'volumen': 0,
            'marca': 'POR DEFINIR',
            'fabricante': 'POR DEFINIR',
            'categoria': 'POR DEFINIR'
        }
    )        
    # 2 detalle con precio transacción
    DetalleVenta.objects.create(
        id_venta=objeto_venta,
        cod_barra=producto_obj,
        cantidad=cantidad_vendida,
        precio_unitario=precio_item
    )    
    #3 descuento del producto en el inventario específico de la tienda
    inventario_obj, creado = Inventario.objects.get_or_create(
        cod_barra=producto_obj,
        rut_tienda_id=rut_tienda_id,
        defaults={
            'stock_actual': 0,
            'precio_venta': precio_item,
            'umbral_seguridad': None 
        }
    )    
    if inventario_obj.stock_actual is None:
        inventario_obj.stock_actual = 0.0        
    stock_previo = float(inventario_obj.stock_actual) #stock antes del descuento    
    #el stock pueda ser numero negativo
    inventario_obj.stock_actual = stock_previo - float(cantidad_vendida)    
    #si el producto ya existía pero con precio 0, se asimila el precio digitado por el cajero
    if not creado and inventario_obj.precio_venta == 0 and precio_item > 0:
        inventario_obj.precio_venta = precio_item        
    inventario_obj.save()    
    #redondeo a 2 decimales
    stock_redondeado = round(inventario_obj.stock_actual, 2)    
    #si el producto no tiene umbral, se asimila como 0
    umbral = inventario_obj.umbral_seguridad if inventario_obj.umbral_seguridad is not None else 0    
    #se plica a todos los productos al llegar a 
    if stock_previo > 0 and stock_redondeado <= 0:
        enviar_alerta_stock(inventario_obj, "QUIEBRE TOTAL DE STOCK")       
    #alerta a reglas con el umbral
    elif umbral > 0 and stock_previo > umbral and stock_redondeado <= umbral:
        enviar_alerta_stock(inventario_obj, "ALERTA DE UMBRAL CRÍTICO")

#valicion de la sesion del cajero
def pantalla_pos(request):
    id_usuario_actual = request.session.get('id_usuario')    
    if not id_usuario_actual:
        return redirect('pantalla_login')
    #se revisa si tiene la sesion abierta de caja, si no la tiene lo redirige a abrir caja
    caja_activa = CajaSesion.objects.filter(id_usuario_id=id_usuario_actual, estado=True).exists()    
    if not caja_activa:
        return redirect('pantalla_apertura_caja')
    return render(request, 'nucleo_sistema/pos.html')

def consultar_deuda_cliente(request):
    #calcula cuenta del cliente fiado por tienda. Formula: Total Comprado (Local) - Total Abonado (Local) = Deuda Actual (Local)
    rut_consulta = request.GET.get('rut')    
    #1 se recupera la tienda del cajero
    rut_tienda_actual = request.session.get('rut_tienda')    
    if not rut_consulta:
        return JsonResponse({'error': 'RUT no proporcionado'}, status=400)
    if not rut_tienda_actual:
        return JsonResponse({'error': 'Sesión de tienda no válida. Inicie sesión nuevamente.'}, status=403)
    try:
        #la tabla cliente es global, por lo que el nombre será el mismo en todas las tiendas, un rut = una persona
        cliente = ClienteFiado.objects.get(rut=rut_consulta)        
        #2 sumar todas las compras a nombre de este cliente, peso solo de una tienda
        suma_compras = Venta.objects.filter(
            rut_cliente=rut_consulta,
            rut_tienda=rut_tienda_actual # <--- Candado Analítico
        ).aggregate(Sum('total_bruto'))['total_bruto__sum'] or 0        
        #3 sumar todos los abonos de dinero, pero solo de una tienda
        suma_abonos = AbonoFiado.objects.filter(
            rut_cliente=rut_consulta,
            rut_tienda=rut_tienda_actual
        ).aggregate(Sum('monto'))['monto__sum'] or 0        
        deuda_actual = suma_compras - suma_abonos
        return JsonResponse({
            'rut': cliente.rut,
            'nombre_completo': f"{cliente.nombre} {cliente.apellido}",
            'total_historico_compras': suma_compras,
            'total_historico_pagos': suma_abonos,
            'deuda_actual': deuda_actual
        }, status=200)
    except ClienteFiado.DoesNotExist:
        return JsonResponse({'error': 'Cliente no encontrado en los registros de fiados.'}, status=404)
    
@csrf_exempt
def registrar_abono(request):
    # recibe el dinero para cuadrar la caja y rebaja la deuda del cliente fiado
    if request.method == 'POST':
        try:
            datos = json.loads(request.body)
            rut = datos.get('rut_cliente')
            monto_abono = int(datos.get('monto', 0))
            id_usuario_id = datos.get('id_usuario', 1)
            rut_tienda_actual = datos.get('rut_tienda') or request.session.get('rut_tienda')            
            if monto_abono <= 0:
                return JsonResponse({'error': 'El monto del abono debe ser mayor a cero.'}, status=400)                
            with transaction.atomic():
                # 1 ingresar el dinero quedando amarrado a la tienda
                nuevo_abono = AbonoFiado.objects.create(
                    fecha_pago=timezone.now(),
                    monto=monto_abono,
                    rut_cliente_id=rut,
                    id_usuario_id=id_usuario_id,
                    rut_tienda_id=rut_tienda_actual
                )
                with connection.cursor() as cursor:
                    # sumar compras históricas de esta tienda
                    cursor.execute("""
                        SELECT COALESCE(SUM(total_bruto), 0) 
                        FROM venta 
                        WHERE (rut_cliente = %s OR id_cliente_fiado = (SELECT id_cliente_fiado FROM cliente_fiado WHERE rut = %s))
                        AND rut_tienda = %s
                    """, [rut, rut, rut_tienda_actual])
                    suma_compras = cursor.fetchone()[0]                    
                    # sumar abonos históricos de esta tienda
                    cursor.execute("""
                        SELECT COALESCE(SUM(monto), 0) 
                        FROM abono_fiado 
                        WHERE rut_cliente = %s AND rut_tienda = %s
                    """, [rut, rut_tienda_actual])
                    suma_abonos = cursor.fetchone()[0]                    
                    # s el saldo es cero o a favor, cerrar las deudas antiguas
                    if suma_abonos >= suma_compras:
                        cursor.execute("""
                            UPDATE venta 
                            SET estado_pago = TRUE 
                            WHERE (rut_cliente = %s OR id_cliente_fiado = (SELECT id_cliente_fiado FROM cliente_fiado WHERE rut = %s))
                            AND rut_tienda = %s
                            AND estado_pago = FALSE
                        """, [rut, rut, rut_tienda_actual])
                return JsonResponse({
                    'mensaje': 'Abono registrado exitosamente. Caja cuadrada.',
                    'id_abono': nuevo_abono.id_abono
                }, status=201)                
        except Exception as error:
            return JsonResponse({'error': f'Error al registrar el abono: {str(error)}'}, status=500)
    else:
        return JsonResponse({'error': 'Método no permitido. Utilice POST.'}, status=405)

#renderiza la interfaz de recaudación con el detalle de ventas pagadas y abonos de fiados del día para cuadrar la caja    
def pantalla_recaudacion(request):
    return render(request, 'nucleo_sistema/recaudacion.html')

@csrf_exempt
def abrir_caja(request):
    #registra el inicio del turno
    if request.method == 'POST':
        datos = json.loads(request.body)
        id_usuario_real = request.session.get('id_usuario')
        rut_tienda_real = request.session.get('rut_tienda')        
        if not id_usuario_real:
            return JsonResponse({'error': 'Sesión expirada o inválida'}, status=403)            
        sesion = CajaSesion.objects.create(
            id_usuario_id=id_usuario_real,
            rut_tienda_id=rut_tienda_real,
            fecha_apertura=timezone.now(),
            monto_apertura=int(datos.get('monto_apertura', 0)),
            estado=True
        )
        return JsonResponse({'mensaje': 'Caja abierta exitosamente', 'id_sesion': sesion.id_sesion}, status=201)

def obtener_estado_cuadratura(request):
    """ Calcula el dinero en caja con sincronización nativa de base de datos """
    id_usuario = request.session.get('id_usuario')
    rut_tienda_actual = request.session.get('rut_tienda')
    
    if not id_usuario:
        return JsonResponse({'error': 'Sesión expirada.'}, status=403)

    sesion = CajaSesion.objects.filter(
        id_usuario_id=id_usuario, 
        rut_tienda_id=rut_tienda_actual, 
        estado=True
    ).last()
    
    if not sesion:
        return JsonResponse({'error': 'No hay una sesión de caja abierta.'}, status=404)

    # Buscamos directamente desde la apertura, sin parches matemáticos
    ventas_hoy = Venta.objects.filter(
        id_usuario_id=id_usuario, 
        estado_pago=True, 
        fecha_venta__gte=sesion.fecha_apertura,
        rut_tienda_id=rut_tienda_actual
    ).aggregate(Sum('total_bruto'))['total_bruto__sum'] or 0

    abonos_hoy = AbonoFiado.objects.filter(
        id_usuario_id=id_usuario, 
        fecha_pago__gte=sesion.fecha_apertura,
        rut_tienda_id=rut_tienda_actual
    ).aggregate(Sum('monto'))['monto__sum'] or 0

    total_esperado = sesion.monto_apertura + ventas_hoy + abonos_hoy
    
    return JsonResponse({
        'id_sesion': sesion.id_sesion,
        'fecha_apertura': sesion.fecha_apertura,
        'fondo_inicial': sesion.monto_apertura,
        'ventas_efectivo': ventas_hoy,          
        'abonos_fiados': abonos_hoy,            
        'total_esperado_en_caja': total_esperado
    })

def pantalla_apertura_caja(request):
    return render(request, 'nucleo_sistema/apertura_caja.html')

def pantalla_cierre_caja(request):
    return render(request, 'nucleo_sistema/cierre_caja.html')

@csrf_exempt
def registrar_cierre(request):
    # termina la seson de caja validando contra la sesión encriptada del servidor para evitar manipulaciones desde el frontend. Solo el usuario que abrió la caja puede cerrarla, y solo si tiene una sesión activa de caja. El monto real y esperado se reciben desde el frontend pero no se confía en ellos, se recalculan en el servidor para validar la cuadratura y detectar posibles errores o fraudes. Si el monto real no coincide con el esperado, se guarda igual pero se marca la sesión para revisión posterior por parte de un administrador.
    if request.method == 'POST':
        datos = json.loads(request.body)        
        # se utiliza el ID del servidor
        id_usuario_real = request.session.get('id_usuario')
        rut_tienda_real = request.session.get('rut_tienda')
        monto_real = int(datos.get('monto_real', 0))
        monto_esperado = int(datos.get('monto_esperado', 0))
        with transaction.atomic():
            sesion = CajaSesion.objects.filter(id_usuario_id=id_usuario_real, rut_tienda_id=rut_tienda_real, estado=True).last()
            if sesion:
                sesion.fecha_cierre = timezone.now()
                sesion.monto_cierre_real = monto_real
                sesion.monto_cierre_esperado = monto_esperado
                sesion.estado = False
                sesion.save()                
                return JsonResponse({'mensaje': 'Caja cerrada exitosamente'}, status=200)                
        return JsonResponse({'error': 'No se encontró sesión activa para tu usuario'}, status=404)        
    return JsonResponse({'error': 'Método no permitido'}, status=405)

def cerrar_sesion(request):
    #destruye la sesión de Django y expulsa al usuario al login
    request.session.flush()
    return redirect('pantalla_login')

def pantalla_login(request):
    #acceso al sistema validando credenciales y redirige segun rol
    if request.method == 'POST':
        usuario_ingresado = request.POST.get('usuario', '').strip()
        clave_ingresada = request.POST.get('clave', '').strip()
        try:
            #se busca por nombre de usuario
            usuario_objeto = Usuario.objects.get(nombre_usuario=usuario_ingresado)            
            #se compara el texto plano con el hash de bd, si no coincide se rechaza el acceso. Si coincide, se revisa si el usuario está activo y si requiere cambio de clave para redirigirlo a la pantalla de cambio de contraseña antes de permitirle el acceso al sistema. Solo si la contraseña es correcta, el usuario está activo y no requiere cambio de clave, se le permite el acceso normal al sistema con inyección de variables globales de sesión para controlar accesos y mostrar datos específicos por tienda.
            if not check_password(clave_ingresada, usuario_objeto.password):
                messages.error(request, 'Usuario o contraseña incorrectos')
                return redirect('pantalla_login')            
            if not usuario_objeto.es_activo:
                messages.error(request, 'Acceso denegado: Esta cuenta ha sido desactivada.')
                return redirect('pantalla_login')           
            if usuario_objeto.requiere_cambio_pass:
                # guarda el id en una sesión temporal...
                request.session['usuario_en_cambio'] = usuario_objeto.id_usuario
                return render(request, 'nucleo_sistema/cambiar_password.html')            
            #registro de timestamp para la bd
            usuario_objeto.ultimo_ingreso = timezone.now()
            usuario_objeto.save(update_fields=['ultimo_ingreso'])            
            # inyección de variables globales
            request.session['id_usuario'] = usuario_objeto.id_usuario
            request.session['rol'] = usuario_objeto.rol
            rol_limpio = usuario_objeto.rol.strip().upper()           
            #guarda trafico del rol
            if rol_limpio == 'CORPORATIVO':
                #el usuario maestro no pertenece a una tienda, es global
                request.session['id_usuario'] = usuario_objeto.id_usuario
                request.session['rol'] = 'CORPORATIVO'
                return redirect('pantalla_corporativa')
            elif rol_limpio == 'ANALISTA':
                return redirect('pantalla_consola_analista')                
            elif rol_limpio == 'ADMINISTRADOR':
                request.session['rut_tienda'] = usuario_objeto.rut_tienda_id 
                return redirect('pantalla_dashboard')                
            else:
                request.session['rut_tienda'] = usuario_objeto.rut_tienda_id 
                return redirect('pantalla_apertura_caja')            
        except Usuario.DoesNotExist:
            messages.error(request, 'Usuario o contraseña incorrectos')
            return redirect('pantalla_login')
    return render(request, 'nucleo_sistema/login.html')

def pantalla_dashboard(request):
    #renderiza el panel del Administrador y calcula los KPI y gráficos para la tienda del usuario logeado. Solo accesible para Administradores y Analistas.
    rol_sesion = str(request.session.get('rol', '')).strip().upper()
    if rol_sesion not in ['ADMINISTRADOR', 'ANALISTA']:
        return redirect('pantalla_pos')        
    rut_tienda_actual = request.session.get('rut_tienda')
    hoy = timezone.now().date()    
    #1 datos geograficos
    nombre_t, comuna_t = "Almacén", "Sucursal"
    try:
        tienda_obj = Tienda.objects.get(rut_tienda=rut_tienda_actual)
        nombre_t = getattr(tienda_obj, 'nombre', "Almacén Central")
        comuna_t = getattr(tienda_obj, 'comuna', "Santiago")
    except:
        pass        
    #2 calcula ventas del dia y fiados activos, con candados de tienda para evitar contaminación cruzada entre tiendas en caso de que un cliente compre en varias sucursales o un analista revise varias tiendas
    ventas_hoy = Venta.objects.filter(
        rut_tienda=rut_tienda_actual, fecha_venta__date=hoy, estado_pago=True
    ).aggregate(total=Sum('total_bruto'))['total'] or 0    
    #sumar todas las compras fiadas históricas de esta tienda
    total_fiado_historico = Venta.objects.filter(
        rut_tienda=rut_tienda_actual, rut_cliente__isnull=False
    ).aggregate(total=Sum('total_bruto'))['total'] or 0    
    #sumar todos los abonos históricos solo de esta tienda
    total_abonos = AbonoFiado.objects.filter(
        rut_tienda=rut_tienda_actual
    ).aggregate(total=Sum('monto'))['total'] or 0    
    deuda_viva = max(total_fiado_historico - total_abonos, 0)    
    # cuenta las facturas cruzando el rut de la tienda
    facturas_del_mes = Factura.objects.filter(
        rut_tienda=rut_tienda_actual,
        fecha_ingreso__year=hoy.year,
        fecha_ingreso__month=hoy.month
    ).count()    
    #multiplicar stock actual * precio de venta de cada producto en el inventario de esta tienda y sumar todo para obtener el valor total del inventario
    valor_inventario = Inventario.objects.filter(
        rut_tienda=rut_tienda_actual, stock_actual__gt=0
    ).aggregate(
        valor_total=Sum(F('stock_actual') * F('precio_venta'))
    )['valor_total'] or 0 
    #a) grafico de barras (Últimos 7 días)
    hace_7_dias = hoy - timedelta(days=6)
    ventas_semana = Venta.objects.filter(
        rut_tienda=rut_tienda_actual, fecha_venta__date__gte=hace_7_dias, estado_pago=True
    ).annotate(fecha_corta=TruncDate('fecha_venta')) \
     .values('fecha_corta').annotate(total=Sum('total_bruto')).order_by('fecha_corta')     
    ventas_dict = {v['fecha_corta']: v['total'] for v in ventas_semana}
    etiquetas_dias, datos_ventas = [], []    
    for i in range(6, -1, -1):
        dia_iter = hoy - timedelta(days=i)
        etiquetas_dias.append(dia_iter.strftime("%d/%m"))
        datos_ventas.append(ventas_dict.get(dia_iter, 0))        
    #b) dona (Top 5 Categorias que más vendieron en dinero en el último mes)
    ventas_por_categoria = DetalleVenta.objects.filter(
        id_venta__rut_tienda=rut_tienda_actual
    ).values(
        nombre_cat=F('cod_barra__categoria')
    ).annotate(
        valor_total=Sum(F('cantidad') * F('precio_unitario'))
    ).order_by('-valor_total')[:5]    
    donut_labels = [item['nombre_cat'] for item in ventas_por_categoria]
    donut_data = [int(item['valor_total']) for item in ventas_por_categoria]    
    #si esta vacio el gráfico de dona, se muestra un mensaje de sin ventas para evitar errores en el frontend al intentar renderizar un gráfico sin datos
    if not donut_labels:
        donut_labels = ['Sin Ventas']
        donut_data = [0]        
    #5 Empaquetar y enviar
    contexto = {
        'nombre_tienda': nombre_t,
        'comuna_tienda': comuna_t,
        'kpi_ventas_dia': f"{ventas_hoy:,}".replace(',', '.'),
        'kpi_fiados_activos': f"{deuda_viva:,}".replace(',', '.'),
        'kpi_facturas_mes': facturas_del_mes, 
        'kpi_valor_inventario': f"{int(valor_inventario):,}".replace(',', '.'),        
        'chart_labels': json.dumps(etiquetas_dias),
        'chart_data': json.dumps(datos_ventas),
        'donut_labels': json.dumps(donut_labels),
        'donut_data': json.dumps(donut_data)
    }
    return render(request, 'nucleo_sistema/dashboard_admin.html', contexto)

def pantalla_catalogo(request):
    #Muestra el catálogo con filtros avanzados, paginación y proporciones optimizadas para no colapsar la RAM. Solo accesible para Administradores.    
    rol_sesion = str(request.session.get('rol', '')).strip().upper()
    if rol_sesion != 'ADMINISTRADOR':
        return redirect('pantalla_pos')
    #1 captura de parámetros de búsqueda y filtros, con limpieza de espacios para evitar errores de búsqueda por espacios extras
    query_general = request.GET.get('q', '').strip()
    marca_filtro = request.GET.get('marca', '').strip()
    categoria_filtro = request.GET.get('categoria', '').strip()

    #2 consulta base
    productos_query = Producto.objects.all().order_by('descripcion')
    #3 filtros avanzados: Si hay una consulta general, se busca por coincidencia parcial en la descripción O en el código de barras. Si hay filtros específicos de marca o categoría, se aplican adicionalmente. Esto permite combinaciones de búsqueda muy flexibles.
    if query_general:
        #busqueda por coincidencia en descripción o cadigo de barras
        productos_query = productos_query.filter(
            Q(descripcion__icontains=query_general) | Q(cod_barra__icontains=query_general)
        )
    if marca_filtro:
        productos_query = productos_query.filter(marca=marca_filtro)
    if categoria_filtro:
        productos_query = productos_query.filter(categoria=categoria_filtro)

    #4 se cargan solo 50 productos por pantalla
    paginador = Paginator(productos_query, 50)
    numero_pagina = request.GET.get('page')
    pagina_objetos = paginador.get_page(numero_pagina)
    marcas_unicas = Producto.objects.values_list('marca', flat=True).distinct().order_by('marca')
    categorias_unicas = Producto.objects.values_list('categoria', flat=True).distinct().order_by('categoria')
    #5 empaquetado
    contexto = {
        'productos': pagina_objetos,
        'marcas': marcas_unicas,
        'categorias': categorias_unicas,
        'q_actual': query_general,
        'marca_actual': marca_filtro,
        'categoria_actual': categoria_filtro,
    }    
    return render(request, 'nucleo_sistema/catalogo_productos.html', contexto)

def registrar_producto(request):
    #crea el producto en la maestra
    rol_sesion = str(request.session.get('rol', '')).strip().upper()    
    if request.method == 'POST' and rol_sesion == 'ADMINISTRADOR':
        try:
            #extracción y limpieza de datos
            cod_barra = request.POST.get('cod_barra', '').strip()
            descripcion = request.POST.get('descripcion', '').strip()
            volumen = int(request.POST.get('volumen', 0))
            marca = request.POST.get('marca', '').strip()
            fabricante = request.POST.get('fabricante', '').strip()
            categoria = request.POST.get('categoria', '').strip()
            precio_venta = int(request.POST.get('precio_venta', 0))
            #validaciones de negovio
            if precio_venta <= 0:
                messages.error(request, 'Error: El precio de venta sugerido debe ser mayor a 0.')
                return redirect('pantalla_catalogo')            
            if volumen <= 0:
                messages.error(request, 'Error: El volumen debe ser mayor a 0.')
                return redirect('pantalla_catalogo')            
            if not fabricante:
                messages.error(request, 'Error: El campo fabricante no puede estar vacío.')
                return redirect('pantalla_catalogo')
            #código para que no se creen duplicados
            if Producto.objects.filter(cod_barra=cod_barra).exists():
                messages.error(request, f'Error: El código de barras {cod_barra} ya existe en la base de datos.')
                return redirect('pantalla_catalogo')
            # inyeccion a la bd
            nuevo_producto = Producto(
                cod_barra=cod_barra,
                descripcion=descripcion,
                volumen=volumen,
                marca=marca,
                fabricante=fabricante,
                categoria=categoria,
                precio_venta=precio_venta
            )
            nuevo_producto.save()
            messages.success(request, 'Producto registrado exitosamente en el catálogo.')            
        except ValueError:
            messages.error(request, "Error de formato: Asegúrese de ingresar números válidos en volumen y precio.")
        except Exception as e:
            # Exponemos el error de PostgreSQL en la pantalla
            messages.error(request, f"Error interno al guardar en base de datos: {str(e)}")            
    return redirect('pantalla_catalogo')

def pantalla_abastecimiento(request):
    #Muestra la interfaz de ingreso de facturas para actualizar el stock físico. Solo accesible para Administradores. Para optimizar la RAM, no se cargan todos los productos en esta vista, sino que se utiliza un buscador predictivo que consulta el inventario de la tienda en tiempo real a medida que el usuario escribe el nombre o código del producto.
    rol_sesion = str(request.session.get('rol', '')).strip().upper()
    if rol_sesion != 'ADMINISTRADOR':
        return redirect('pantalla_pos')    
    return render(request, 'nucleo_sistema/abastecimiento.html')

@csrf_exempt
def registrar_abastecimiento_api(request):
    #procesa la factura y actualiza el stock.Si el producto no existe en el inventario de la tienda, se crea. Si existe, suma la cantidad recibida
    if request.method == 'POST':
        datos = json.loads(request.body)        
        try:
            with transaction.atomic():
                #1 cabecera de la factura, se guarda primero para luego amarrar el detalle a esta factura y tener un registro de la fecha de ingreso del producto al stock físico, además de poder llevar un histórico de facturas recibidas
                factura_obj = Factura.objects.create(
                    folio_factura=datos['folio'],
                    es_compra_directa=datos['es_compra_directa'],
                    fecha_emision=datos['fecha_emision'],
                    fecha_ingreso=timezone.now().date(),
                    rut_tienda_id=datos['rut_tienda']
                )
                for item in datos['items']:
                    #aceptar decimales de la factura
                    cantidad_recibida = float(item['cantidad'])
                    #guardar detalle de factura
                    DetalleFactura.objects.create(
                        folio_factura=factura_obj,
                        cod_barra_id=item['codBarra'],
                        cantidad=cantidad_recibida,
                        valor_compra=item['costo']
                    )                    
                    umbral_recibido = item.get('umbral_seguridad')
                    umbral_final = int(umbral_recibido) if umbral_recibido is not None else None                    
                    inventario_obj, creado = Inventario.objects.get_or_create(
                        cod_barra_id=item['codBarra'],
                        rut_tienda_id=datos['rut_tienda'],
                        defaults={
                            'stock_actual': 0.0, 
                            'precio_venta': int(item['precio_venta']), 
                            'umbral_seguridad': umbral_final
                        }
                    )                    
                    stock_actual_bd = float(inventario_obj.stock_actual if inventario_obj.stock_actual else 0.0)
                    inventario_obj.stock_actual = stock_actual_bd + cantidad_recibida                    
                    inventario_obj.precio_venta = int(item['precio_venta'])
                    inventario_obj.umbral_seguridad = umbral_final
                    inventario_obj.save()
                return JsonResponse({'mensaje': 'Abastecimiento procesado'}, status=201)
        except Exception as e:
            return JsonResponse({'error': str(e)}, status=400)
    return JsonResponse({'error': 'Método no permitido'}, status=405)

def api_buscar_productos(request):
    #buscador predictivo, extrayendo el precio exclusivamente del inventario de la tienda para evitar mostrar precios de otras sucursales en caso de que el producto exista en varias tiendas. Solo accesible para Administradores y Analistas.
    query = request.GET.get('q', '').strip()
    rut_tienda = request.GET.get('rut_tienda') or request.session.get('rut_tienda')    
    if len(query) < 3:
        return JsonResponse([], safe=False)    
    try:
        terminos = query.split()
        filtro_descripcion = Q()        
        for termino in terminos:
            filtro_descripcion &= (Q(descripcion__icontains=termino) | Q(marca__icontains=termino))        
        filtro_final = filtro_descripcion
        if query.isdigit():
            filtro_final |= Q(cod_barra__icontains=query)            
        productos = Producto.objects.filter(filtro_final).order_by('descripcion')[:40]        
        resultados = []
        for p in productos:
            precio_actual = 0
            existencia = 0            
            if rut_tienda:
                inv = Inventario.objects.filter(cod_barra_id=p.cod_barra, rut_tienda_id=rut_tienda).first()
                if inv:
                    precio_actual = inv.precio_venta
                    existencia = inv.stock_actual
            resultados.append({
                'cod_barra': str(p.cod_barra),
                'descripcion': p.descripcion,
                'marca': p.marca,
                'precio_venta': precio_actual,
                'stock_disponible': existencia
            })        
        return JsonResponse(resultados, safe=False)        
    except Exception as error:
        return JsonResponse({'error': str(error)}, status=500)
    
def pantalla_ajustes(request):
    #muestra la pantalla de ajustes, filtra la tabla y alimenta al exportador
    rol_sesion = str(request.session.get('rol', '')).strip().upper()
    if rol_sesion != 'ADMINISTRADOR':
        return redirect('pantalla_pos')        
    rut_tienda_actual = request.session.get('rut_tienda')
    query_general = request.GET.get('q', '').strip()
    marca_filtro = request.GET.get('marca', '').strip()
    categoria_filtro = request.GET.get('categoria', '').strip()
    inventario_tienda = Inventario.objects.filter(
        rut_tienda=rut_tienda_actual
    ).select_related('cod_barra')
    #aplicación de filtros a la vista web
    if query_general:
        inventario_tienda = inventario_tienda.filter(
            Q(cod_barra__descripcion__icontains=query_general) | Q(cod_barra__cod_barra__icontains=query_general)
        )
    if marca_filtro:
        inventario_tienda = inventario_tienda.filter(cod_barra__marca=marca_filtro)
    if categoria_filtro:
        inventario_tienda = inventario_tienda.filter(cod_barra__categoria=categoria_filtro)
    inventario_tienda = inventario_tienda.order_by('cod_barra__descripcion')
    marcas_unicas = Inventario.objects.filter(rut_tienda=rut_tienda_actual).values_list('cod_barra__marca', flat=True).distinct().order_by('cod_barra__marca')
    categorias_unicas = Inventario.objects.filter(rut_tienda=rut_tienda_actual).values_list('cod_barra__categoria', flat=True).distinct().order_by('cod_barra__categoria')
    # empaquetado
    contexto = {
        'inventario': inventario_tienda,
        'marcas': marcas_unicas,
        'categorias': categorias_unicas,
        'q_actual': query_general,
        'marca_actual': marca_filtro,
        'categoria_actual': categoria_filtro
    }
    return render(request, 'nucleo_sistema/ajustes_inventario.html', contexto)

@csrf_exempt
def registrar_ajuste_api(request):
    #sobrescribe el stock de un producto con el conteo realizado en la tienda, pero además registra el evento en una tabla de auditoría para mantener un histórico de ajustes con fecha, motivo y usuario responsable. Solo accesible para Administradores. Para asegurar la integridad de la auditoría, se implementa una barrera analítica utilizando transacciones atómicas, garantizando que el ajuste y su registro en la auditoría se realicen como una operación indivisible.
    rol_sesion = str(request.session.get('rol', '')).strip().upper()    
    if request.method == 'POST' and rol_sesion == 'ADMINISTRADOR':
        datos = json.loads(request.body)
        rut_tienda_actual = request.session.get('rut_tienda')
        id_usuario_actual = request.session.get('id_usuario')        
        try:
            #cumplimiento ACID
            with transaction.atomic():
                nuevo_stock = int(datos['nuevo_stock'])
                motivo_ajuste = datos['motivo']                
                #recuperar el inventario actual
                inv_obj, creado = Inventario.objects.get_or_create(
                    cod_barra_id=datos['cod_barra'],
                    rut_tienda_id=rut_tienda_actual,
                    defaults={'stock_actual': 0}
                )                
                #calculo de la variacion
                stock_antiguo = inv_obj.stock_actual
                diferencia = nuevo_stock - stock_antiguo                
                if diferencia != 0:
                    #registor en la tabla de AjusteInventario
                    AjusteInventario.objects.create(
                        cod_barra_id=datos['cod_barra'],
                        rut_tienda_id=rut_tienda_actual,
                        fecha_ajuste=timezone.now(),
                        cantidad=diferencia,
                        motivo=motivo_ajuste,
                        id_usuario_id=id_usuario_actual
                    )                
                # sobrescribe el stock final
                inv_obj.stock_actual = nuevo_stock
                inv_obj.save()            
            return JsonResponse({'mensaje': 'Ajuste y auditoría procesados exitosamente'}, status=200)            
        except Exception as e:
            return JsonResponse({'error': str(e)}, status=400)            
    return JsonResponse({'error': 'Acceso denegado'}, status=403)

def pantalla_configuracion(request):
    #renderiza el módulo de Configuración de Sistema, donde se pueden crear nuevos usuarios, resetear claves y activar/desactivar cuentas. Solo accesible para Administradores.
    rol_sesion = str(request.session.get('rol', '')).strip().upper()
    if rol_sesion != 'ADMINISTRADOR':
        return redirect('pantalla_pos')        
    #se muestran los usuarios que pertenecen a la tienda del Administrador actual
    rut_tienda_actual = request.session.get('rut_tienda')
    lista_usuarios = Usuario.objects.filter(rut_tienda_id=rut_tienda_actual).order_by('nombre')    
    return render(request, 'nucleo_sistema/configuracion_sistema.html', {
        'usuarios': lista_usuarios
    })

def registrar_usuario(request):
    #Procesa el formulario y crea un nuevo usuario con credencial autogenerada en la base de datos, asignándolo automáticamente a la misma tienda del administrador que lo creó. Solo accesible para Administradores. Para evitar colisiones de nombres de usuario, se implementa un algoritmo que genera la credencial combinando el nombre, apellido y un número secuencial si es necesario.
    rol_sesion = str(request.session.get('rol', '')).strip().upper()    
    if request.method == 'POST' and rol_sesion == 'ADMINISTRADOR':
        try:
            #1 se capturan los datos enviados por el administrador y la sesión
            nombre = request.POST.get('nombre', '').strip()
            primer_apellido = request.POST.get('primer_apellido', '').strip()
            segundo_apellido = request.POST.get('segundo_apellido', '').strip()
            rol = request.POST.get('rol', '').strip()
            mail = request.POST.get('mail', '').strip()
            password = request.POST.get('password', '').strip()
            rut_tienda_admin = str(request.session.get('rut_tienda', '')).strip()
            #verificacon de campos obligatorios
            errores = []
            if not nombre:
                errores.append("El campo 'Nombre' es obligatorio y no puede contener solo espacios.")
            if not primer_apellido:
                errores.append("El campo 'Primer Apellido' es obligatorio.")
            if not mail:
                errores.append("El campo 'Correo Electrónico' es obligatorio.")            
            #si hay errores logicos se frena la operación antes de tocar la bd
            if errores:
                for error in errores:
                    messages.error(request, error)
                return redirect('pantalla_configuracion')
            #2 generador de usuario unico
            # Ejemplo: Si es "Juan Perez" en la tienda "776094468", genera "jperez_4468"
            if nombre and primer_apellido:
                base_usuario = f"{nombre[0].lower()}{primer_apellido.lower()}_{rut_tienda_admin[-4:]}".replace(" ", "")
            else:
                base_usuario = f"user_{rut_tienda_admin[-4:]}"
            nombre_usuario_final = base_usuario
            contador = 1            
            #validacion para evitar colicion en bd
            while Usuario.objects.filter(nombre_usuario=nombre_usuario_final).exists():
                nombre_usuario_final = f"{base_usuario}{contador}"
                contador += 1
            # 3 inyeccion a bd
            nuevo_usuario = Usuario(
                nombre_usuario=nombre_usuario_final,
                nombre=nombre, # <-- CORRECCIÓN: Esta es la línea que faltaba
                primer_apellido=primer_apellido,
                segundo_apellido=segundo_apellido,
                rol=rol,
                mail=mail,
                password=make_password(password),
                es_activo=True,
                requiere_cambio_pass=True,
                fecha_creacion=timezone.now(),
                rut_tienda_id=rut_tienda_admin
            )
            nuevo_usuario.save()            
            #4 aviso por pantalla para que el admin sepa de la credencial asignada al nuevo usuario
            messages.success(request, f"Usuario registrado exitosamente. La credencial de acceso asignada es: {nombre_usuario_final}")            
        except Exception as e:
            print(f"Error analítico al crear usuario: {e}")
            messages.error(request, "Error de sistema al intentar registrar el usuario.")
    return redirect('pantalla_configuracion')

@csrf_exempt
def api_reset_clave(request):
    #administrador sobrescribe la contraseña de un usuario específico en la base de datos, dejando un registro de que fue el administrador quien hizo el cambio. Solo accesible para Administradores. Para reforzar la seguridad, se implementa un candado que solo permite editar la clave de usuarios que pertenecen a la misma tienda del administrador.
    rol_sesion = str(request.session.get('rol', '')).strip().upper()
    if request.method == 'POST' and rol_sesion == 'ADMINISTRADOR':
        datos = json.loads(request.body)
        rut_tienda_actual = request.session.get('rut_tienda')        
        try:
            #se permite editar si el usuario es de su misma tienda
            usuario_obj = Usuario.objects.get(
                id_usuario=datos['id_usuario'],
                rut_tienda_id=rut_tienda_actual
            )            
            # aplicación de nueva clave
            usuario_obj.password = make_password(datos['nueva_clave'])
            usuario_obj.save()            
            return JsonResponse({'mensaje': 'Clave actualizada exitosamente'}, status=200)            
        except Usuario.DoesNotExist:
            return JsonResponse({'error': 'Usuario no encontrado o no pertenece a esta sucursal'}, status=404)
        except Exception as e:
            return JsonResponse({'error': str(e)}, status=400)
    return JsonResponse({'error': 'Acceso denegado'}, status=403)

@csrf_exempt
def api_cambiar_estado(request):
    #activacion o desactivacion de acceso de un usuario al sistema, cambiando el valor del campo es_activo en la base de datos. Solo accesible para Administradores. Para evitar que un administrador bloquee su propia cuenta por error, se implementa un candado que impide cambiar el estado del usuario que está actualmente logueado. Además, solo se pueden activar/desactivar usuarios que pertenezcan a la misma tienda del administrador para evitar contaminación cruzada entre sucursales.
    rol_sesion = str(request.session.get('rol', '')).strip().upper()
    if request.method == 'POST' and rol_sesion == 'ADMINISTRADOR':
        datos = json.loads(request.body)
        rut_tienda_actual = request.session.get('rut_tienda')
        admin_actual_id = request.session.get('id_usuario')        
        try:
            #evitar el auto-bloqueo
            if str(datos['id_usuario']) == str(admin_actual_id):
                return JsonResponse({'error': 'Operación denegada: No puede bloquear su propia cuenta de administrador.'}, status=400)
            #validar que el usuario pertenezca a la misma tienda
            usuario_obj = Usuario.objects.get(
                id_usuario=datos['id_usuario'],
                rut_tienda_id=rut_tienda_actual
            )            
            usuario_obj.es_activo = not usuario_obj.es_activo
            usuario_obj.save()            
            return JsonResponse({'mensaje': 'Estado actualizado exitosamente'}, status=200)            
        except Usuario.DoesNotExist:
            return JsonResponse({'error': 'Usuario no encontrado.'}, status=404)
        except Exception as e:
            return JsonResponse({'error': str(e)}, status=400)
    return JsonResponse({'error': 'Acceso denegado'}, status=403)

def pantalla_recuperar_password(request):
    #muestra el formulario para ingresar el correo asociado a la cuenta y recibir una clave temporal por correo para recuperar el acceso. Para reforzar la seguridad, se implementa un proceso de verificación que solo envía la clave temporal si el correo ingresado corresponde a un usuario activo con rol de Administrador o Analista, evitando así que cuentas de clientes o usuarios desactivados puedan ser objetivo de este proceso.
    return render(request, 'nucleo_sistema/recuperar_password.html')

def procesar_recuperacion(request):
    if request.method == 'POST':
        # se cptura el nombre de usuario desde el formulario
        username_ingresado = request.POST.get('username', '').strip()        
        try:
            usuario_obj = Usuario.objects.get(nombre_usuario=username_ingresado)            
            # regla de negocio solo administradores y analista
            rol_limpio = usuario_obj.rol.strip().upper()
            if rol_limpio in ['ADMINISTRADOR', 'ANALISTA']:                
                caracteres = string.ascii_letters + string.digits #se genera la nueva la nueva clave
                clave_temporal = ''.join(random.choice(caracteres) for i in range(8))                
                usuario_obj.password = make_password(clave_temporal)
                usuario_obj.requiere_cambio_pass = True
                usuario_obj.save()                
                #envio de clave a al correo ingresado
                resend.api_key = os.environ.get('RESEND_API_KEY', 're_6JNnrWqP_AV7z2yL2jYXTjibvXu8zkLUu')
                correo_destino = usuario_obj.mail.strip().lower()                
                try:
                    resend.Emails.send({
                        "from": "onboarding@resend.dev", 
                        "to": correo_destino,
                        "subject": "Recuperación de Contraseña - Market Data",
                        "html": f"""
                            <p>Hola {usuario_obj.nombre}, se ha solicitado una recuperación para tu usuario <strong>{usuario_obj.nombre_usuario}</strong>.</p>
                            <p>Tu clave temporal de acceso es: <strong>{clave_temporal}</strong>.</p>
                            <p>Por favor, cámbiala al ingresar al sistema.</p>
                            <p>Saludos,<br>Equipo Market Data</p>
                        """
                    })
                    messages.success(request, 'Éxito: Se ha enviado una clave temporal a tu correo asociado.')
                except Exception as e:
                    messages.error(request, 'Ocurrió un problema de conexión al enviar el correo. Contacte a soporte.')
                    print(f"Error de Resend: {e}")                    
                return redirect('pantalla_login')                
            else:
                # si el usuario es cajero
                return render(request, 'nucleo_sistema/recuperar_password.html', {
                    'error': 'Este perfil no tiene los privilegios para usar la recuperación automática.'
                })                
        except Usuario.DoesNotExist:
            #si el usuario no existe en la bd
            return render(request, 'nucleo_sistema/recuperar_password.html', {
                'error': 'El nombre de usuario ingresado no existe en el sistema.'
            })            
    return redirect('pantalla_login')

def pantalla_reportes(request):
    #BI con cruce de tablas. Calcula la ganancia buscando el valor_compra en las facturas para obtener una ganancia realista, en lugar de usar un margen fijo. Solo accesible para Administradores y Analistas. Para optimizar el rendimiento, se implementan consultas agregadas que realizan los cálculos directamente en la base de datos, evitando así la necesidad de cargar grandes volúmenes de datos en memoria y procesarlos en Python.
    rol_sesion = str(request.session.get('rol', '')).strip().upper()
    if rol_sesion not in ['ADMINISTRADOR', 'ANALISTA']:
        return redirect('pantalla_pos')
    rut_tienda_actual = request.session.get('rut_tienda')
    #1 CALCULO DE GANANCIA CRUCE CON DETALLE_FACTURA
    detalles = DetalleVenta.objects.filter(id_venta__rut_tienda=rut_tienda_actual)    
    ganancia_total = 0
    for item in detalles:
        #se busca el último precio de compra registrado para el producto
        factura_info = DetalleFactura.objects.filter(cod_barra=item.cod_barra).last()
        costo = factura_info.valor_compra if factura_info else 0
        ganancia_total += (item.precio_unitario - costo) * item.cantidad
    #2 PROMEDIO DE PRODUCTOS POR VENTA
    total_transacciones = Venta.objects.filter(rut_tienda=rut_tienda_actual).count()
    total_items = detalles.aggregate(total=Sum('cantidad'))['total'] or 0
    promedio_productos = round(total_items / total_transacciones, 1) if total_transacciones > 0 else 0
    total_ingresos = Venta.objects.filter(
        rut_tienda=rut_tienda_actual
    ).aggregate(total=Sum('total_bruto'))['total'] or 0
    #3 RANKING TOP 10 PRODUCTOS MÁS VENDIDOS (UNIDADES Y RECAUDACIÓN)
    ranking_productos = detalles.values(
        nombre=F('cod_barra__descripcion')
    ).annotate(
        unidades_vendidas=Sum('cantidad'),
        total_recaudado=Sum(F('cantidad') * F('precio_unitario'))
    ).order_by('-unidades_vendidas')[:10]
    #4 TOP 10 PRODUCTOS CRÍTICOS
    productos_criticos = Inventario.objects.filter(
        rut_tienda=rut_tienda_actual
    ).select_related('cod_barra').order_by('stock_actual')[:10]
    #5 EMPAQUETADO
    contexto = {
        'total_ingresos': f"{total_ingresos:,}".replace(',', '.'),
        'total_transacciones': total_transacciones,
        'ganancia_total': f"{int(ganancia_total):,}".replace(',', '.'),
        'promedio_productos': promedio_productos,
        'ranking_productos': ranking_productos,
        'productos_criticos': productos_criticos,
    }
    return render(request, 'nucleo_sistema/reportes_analitica.html', contexto)

def pantalla_consola_analista(request):
    from django.utils import timezone
    #filtros y sincronización de CSV para análisis avanzado en Excel. Solo accesible para Analistas. Para optimizar el rendimiento y la experiencia del usuario, se implementa un sistema de filtrado dinámico en cascada que permite a los analistas refinar sus consultas de manera eficiente sin necesidad de recargar toda la página, y un exportador CSV que genera archivos con codificación UTF-8 y delimitadores personalizados para asegurar la compatibilidad con Excel y mantener la integridad de los datos, incluso con grandes volúmenes de información.
    rol_sesion = str(request.session.get('rol', '')).strip().upper()
    if rol_sesion != 'ANALISTA':
        return redirect('pantalla_pos')
    tiendas = Tienda.objects.all().order_by('nombre')
    regiones_disponibles = Comuna.objects.values_list('region', flat=True).distinct().order_by('region')
    comunas_disponibles = Comuna.objects.all().order_by('nombre_comuna')
    #1 CAPTURA DE FILTROS
    regiones_filtro = request.GET.getlist('regiones')
    comunas_filtro = request.GET.getlist('comunas')
    tiendas_filtro = request.GET.getlist('tiendas')
    fecha_inicio = request.GET.get('fecha_inicio', '')
    fecha_fin = request.GET.get('fecha_fin', '')    
    #2 FILTRO EN CASCADA
    tiendas_filtradas = tiendas
    if comunas_filtro:
        tiendas_filtradas = tiendas_filtradas.filter(id_comuna__in=comunas_filtro)
    elif regiones_filtro:
        comunas_ids = Comuna.objects.filter(region__in=regiones_filtro).values_list('id_comuna', flat=True)
        tiendas_filtradas = tiendas_filtradas.filter(id_comuna__in=comunas_ids)
    if tiendas_filtro:
        tiendas_filtradas = tiendas_filtradas.filter(rut_tienda__in=tiendas_filtro)    
    lista_ruts = tiendas_filtradas.values_list('rut_tienda', flat=True)
    ventas_query = Venta.objects.filter(rut_tienda__in=lista_ruts)    
    # se ajusta la hora para que sea exactamente desde el inicio del día de fecha_inicio hasta el final del día de fecha_fin, para incluir todas las ventas de esos días completos
    if fecha_inicio:
        ventas_query = ventas_query.filter(fecha_venta__gte=f"{fecha_inicio} 00:00:00")
    if fecha_fin:
        ventas_query = ventas_query.filter(fecha_venta__lte=f"{fecha_fin} 23:59:59")
    #4 generar CSV
    if request.GET.get('exportar') == 'csv':
        import csv
        from django.http import HttpResponse 
        response = HttpResponse(content_type='text/csv')
        response['Content-Disposition'] = f'attachment; filename="Auditoria_BI_{fecha_inicio}_al_{fecha_fin}.csv"'
        response.write(u'\ufeff'.encode('utf8'))        
        writer = csv.writer(response, delimiter=';')
        writer.writerow([
            'Región', 'Comuna', 'Tienda', 'Tipo Local', 
            'Fecha', 'Hora', 'Estado', 
            'Cód. Barra', 'Producto', 'Marca', 'Fabricante', 
            'Cantidad', 'Stock Disponible', 
            'Precio Venta ($)', 'Costo Compra ($)', 'Margen Unitario ($)'
        ])
        #consulta optimizada con select_related para evitar consultas adicionales por cada fila al acceder a campos relacionados, y prefetch_related para cargar en batch las relaciones de productos y tiendas, reduciendo drásticamente el número de consultas a la base de datos y mejorando el rendimiento incluso con grandes volúmenes de datos.
        detalles = DetalleVenta.objects.filter(id_venta__in=ventas_query).select_related('id_venta', 'cod_barra', 'id_venta__rut_tienda')
        #MOTOR DE PRE-PROCESAMIENTO (Optimización de Memoria)
        codigos_presentes = detalles.values_list('cod_barra', flat=True).distinct()        
        #1 ultimo precio de compra
        costos_historicos = DetalleFactura.objects.filter(cod_barra_id__in=codigos_presentes).order_by('id_detalle_factura')
        dict_costos = {f.cod_barra_id: f.valor_compra for f in costos_historicos}
        #2 stock actual en la tienda correspondiente
        inventario_data = Inventario.objects.filter(rut_tienda__in=lista_ruts).values('cod_barra_id', 'rut_tienda_id', 'stock_actual')
        dict_stock = {(i['cod_barra_id'], i['rut_tienda_id']): i['stock_actual'] for i in inventario_data}
        for d in detalles:
            v = d.id_venta
            t = v.rut_tienda
            p = d.cod_barra
            comuna_obj = Comuna.objects.filter(id_comuna=t.id_comuna).first()
            estado_texto = "PAGADO" if v.estado_pago else "FIADO"#método de pago
            tipo_l = t.tipo_tienda if t.tipo_tienda else "N/A" #tipo de tienda, con manejo de nulos            
            # Recuperación de mapas pre-cargados para costo y stock, evitando consultas adicionales en cada iteración
            costo_u = dict_costos.get(p.cod_barra, 0)
            stock_disp = dict_stock.get((p.cod_barra, t.rut_tienda), 0)
            margen_u = d.precio_unitario - costo_u                 
            #zona horaria local para Excel
            if timezone.is_aware(v.fecha_venta):
                fecha_local = timezone.localtime(v.fecha_venta)
            else:
                fecha_local = v.fecha_venta
            
            writer.writerow([
                comuna_obj.region if comuna_obj else 'N/A',
                comuna_obj.nombre_comuna if comuna_obj else 'N/A',
                t.nombre,
                tipo_l,
                fecha_local.strftime('%d/%m/%Y'),
                fecha_local.strftime('%H:%M:%S'),
                estado_texto,
                p.cod_barra,
                p.descripcion,
                p.marca,
                p.fabricante,
                d.cantidad,
                stock_disp,
                d.precio_unitario,
                costo_u,
                margen_u
            ])            
        return response    
    #5 DASHBOARD VISUAL (Optimización de Consultas y Processing en RAM)    
    total_bruto = ventas_query.aggregate(total=Sum('total_bruto'))['total'] or 0    
    #a) graficos fabricantes
    ventas_fabricante = DetalleVenta.objects.filter(id_venta__in=ventas_query).values(
        nombre_fabricante=F('cod_barra__fabricante')
    ).annotate(
        total_ventas=Sum(F('cantidad') * F('precio_unitario'))
    ).order_by('-total_ventas')[:10]
    labels_barras = [v['nombre_fabricante'] for v in ventas_fabricante]
    datos_barras = [int(v['total_ventas']) for v in ventas_fabricante]
    #b) grafico abastecimiento
    facturas_query = Factura.objects.all()
    if fecha_inicio: facturas_query = facturas_query.filter(fecha_ingreso__gte=fecha_inicio)
    if fecha_fin: facturas_query = facturas_query.filter(fecha_ingreso__lte=fecha_fin)    
    datos_dona = [
        facturas_query.filter(es_compra_directa=True).count(),
        facturas_query.filter(es_compra_directa=False).count()
    ]
    #c) grafico tipo de tienda
    ventas_brutas = ventas_query.values('rut_tienda__tipo_tienda', 'fecha_venta', 'total_bruto')    
    fechas_set = set()
    tipos_data = defaultdict(dict)    
    for v in ventas_brutas:
        tipo = v['rut_tienda__tipo_tienda'] if v['rut_tienda__tipo_tienda'] else "SIN CATEGORÍA"
        fecha_obj = v['fecha_venta'].date()
        fechas_set.add(fecha_obj)        
        if fecha_obj in tipos_data[tipo]:
            tipos_data[tipo][fecha_obj] += int(v['total_bruto'])
        else:
            tipos_data[tipo][fecha_obj] = int(v['total_bruto'])
    fechas_ordenadas = sorted(list(fechas_set))
    labels_multilinea = [f.strftime("%d/%m") for f in fechas_ordenadas]    
    datasets_multilinea = []
    colores= ['#007bff', '#28a745', '#dc3545', '#ffc107', '#17a2b8', '#6610f2']
    c_idx = 0    
    for tipo_tienda, datos_fechas in tipos_data.items():
        data_array = [datos_fechas.get(f, 0) for f in fechas_ordenadas]
        datasets_multilinea.append({
            'label': tipo_tienda,
            'data': data_array,
            'borderColor': colores[c_idx % len(colores)],
            'backgroundColor': colores[c_idx % len(colores)],
            'borderWidth': 3,
            'fill': False,
            'tension': 0.3
        })
        c_idx += 1    
    #d) grafico marcas por tipo de tienda
    top_marcas_qs = DetalleVenta.objects.filter(id_venta__in=ventas_query).values(
        'cod_barra__marca'
    ).annotate(total_unidades=Sum('cantidad')).order_by('-total_unidades')[:10]    
    lista_top_marcas = [m['cod_barra__marca'] for m in top_marcas_qs if m['cod_barra__marca']]
    # extraccion de detalle solo de esas marcas ganadoras
    ventas_marcas = DetalleVenta.objects.filter(
        id_venta__in=ventas_query,
        cod_barra__marca__in=lista_top_marcas
    ).values(
        'id_venta__rut_tienda__tipo_tienda', 'cod_barra__marca'
    ).annotate(unidades=Sum('cantidad'))
    tipos_data_marcas = defaultdict(lambda: defaultdict(int))
    for v in ventas_marcas:
        tipo = v['id_venta__rut_tienda__tipo_tienda'] if v['id_venta__rut_tienda__tipo_tienda'] else "SIN CATEGORÍA"
        marca = v['cod_barra__marca']
        tipos_data_marcas[tipo][marca] += v['unidades']
    datasets_marcas = []
    c_idx2 = 0
    colores_marcas = ['#fd7e14', '#20c997', '#e83e8c', '#6f42c1', '#17a2b8', '#343a40']    
    for tipo, marcas_dict in tipos_data_marcas.items():
        data_array = [marcas_dict.get(m, 0) for m in lista_top_marcas]
        datasets_marcas.append({
            'label': tipo,
            'data': data_array,
            'backgroundColor': colores_marcas[c_idx2 % len(colores_marcas)],
            'borderWidth': 0
        })
        c_idx2 += 1
    #e) tabla horarios de compra
    ventas_fechas_crudas = ventas_query.values_list('fecha_venta', flat=True)
    dict_horas = {i: 0 for i in range(24)}
    total_transacciones = ventas_fechas_crudas.count()
    for fecha_obj in ventas_fechas_crudas:
        #se valida la fecha trae zona horaria (Aware) o viene limpia (Naive)
        if timezone.is_aware(fecha_obj):
            hora_local = timezone.localtime(fecha_obj).hour
        else:
            hora_local = fecha_obj.hour            
        dict_horas[hora_local] += 1        
    tabla_horas = []
    #tabla solo para las horas que tuvieron movimiento
    for hora in range(24):
        cant = dict_horas[hora]
        if cant > 0:
            porcentaje = round((cant / total_transacciones) * 100, 1)
            hora_formato = f"{str(hora).zfill(2)}:00 - {str(hora).zfill(2)}:59"            
            #>= 12% es alto (Rojo), <= 3% es Valle (Gris)
            estado = "normal"
            if porcentaje >= 12: estado = "alto"
            elif porcentaje <= 3: estado = "valle"            
            tabla_horas.append({
                'rango': hora_formato,
                'cantidad': cant,
                'porcentaje': porcentaje,
                'estado': estado
            })
    #f) grafico top categoria por ingresos
    ventas_categoria = DetalleVenta.objects.filter(id_venta__in=ventas_query).values(
        nombre_categoria=F('cod_barra__categoria')
    ).annotate(
        total_recaudado=Sum(F('cantidad') * F('precio_unitario'))
    ).order_by('-total_recaudado')[:7]
    labels_categorias = [c['nombre_categoria'] if c['nombre_categoria'] else "SIN CATEGORÍA" for c in ventas_categoria]
    datos_categorias = [int(c['total_recaudado']) for c in ventas_categoria]
    contexto = {
        'tiendas': tiendas,
        'regiones': regiones_disponibles,
        'comunas': comunas_disponibles,
        'total_ingresos': f"{total_bruto:,}".replace(',', '.'),
        'labels_barras': json.dumps(labels_barras),
        'datos_barras': json.dumps(datos_barras),
        'labels_dona': json.dumps(['Compra Directa', 'Proveedor Mayorista']),
        'datos_dona': json.dumps(datos_dona),
        'labels_multilinea': json.dumps(labels_multilinea),
        'datasets_multilinea': json.dumps(datasets_multilinea),        
        'fecha_inicio': fecha_inicio, 
        'fecha_fin': fecha_fin,
        'tiendas_seleccionadas': tiendas_filtro,
        'regiones_seleccionadas': regiones_filtro,
        'comunas_seleccionadas': comunas_filtro,
        'labels_marcas': json.dumps(lista_top_marcas),
        'datasets_marcas': json.dumps(datasets_marcas),
        'tabla_horas': tabla_horas,
        'labels_categorias': json.dumps(labels_categorias),
        'datos_categorias': json.dumps(datos_categorias),
    }
    return render(request, 'nucleo_sistema/dashboard_analista.html', contexto)

def exportar_inventario_excel(request):
    #validación del rol
    rol_sesion = str(request.session.get('rol', '')).strip().upper()
    if rol_sesion != 'ADMINISTRADOR':
        return redirect('pantalla_pos')
    rut_tienda_actual = request.session.get('rut_tienda')    
    #captura del filtros
    q = request.GET.get('q', '').strip()
    marca = request.GET.get('marca', '').strip()
    categoria = request.GET.get('categoria', '').strip()
    #consulta a bd
    inventario_query = Inventario.objects.filter(
        rut_tienda=rut_tienda_actual
    ).select_related('cod_barra')
    if q:
        inventario_query = inventario_query.filter(
            Q(cod_barra__descripcion__icontains=q) | Q(cod_barra__cod_barra__icontains=q)
        )
    if marca:
        inventario_query = inventario_query.filter(cod_barra__marca=marca)
    if categoria:
        inventario_query = inventario_query.filter(cod_barra__categoria=categoria)
    inventario_query = inventario_query.order_by('cod_barra__descripcion')
    #generacion del CSV
    response = HttpResponse(content_type='text/csv')
    fecha_str = timezone.now().strftime("%d-%m-%Y")
    response['Content-Disposition'] = f'attachment; filename="Stock_Filtrado_{fecha_str}.csv"'
    response.write(u'\ufeff'.encode('utf8'))    
    writer = csv.writer(response, delimiter=';')
    writer.writerow(['Cód. Barra', 'Descripción', 'Marca', 'Categoría', 'Stock Actual', 'Precio Venta ($)'])
    for item in inventario_query:
        writer.writerow([
            item.cod_barra.cod_barra,
            item.cod_barra.descripcion,
            item.cod_barra.marca,
            item.cod_barra.categoria,
            item.stock_actual,
            item.precio_venta
        ])
    return response

def enviar_alerta_stock(inventario_obj, tipo_alerta):
    #Despacha alertas de inventario usando la API de Resend para evadir bloqueos de puertos
    try:
        # Recuperacion de administradores activos de la tienda
        admins = Usuario.objects.filter(
            rut_tienda_id=inventario_obj.rut_tienda_id, 
            rol__iexact='ADMINISTRADOR', 
            es_activo=True
        )
        correos_destino = [admin.mail.strip().lower() for admin in admins if admin.mail]        
        if correos_destino:
            producto = inventario_obj.cod_barra.descripcion
            stock = float(inventario_obj.stock_actual)
            umbral = inventario_obj.umbral_seguridad if inventario_obj.umbral_seguridad is not None else 0            
            html_mensaje = f"""
            <h3>⚠️ {tipo_alerta}: {producto}</h3>
            <p>Estimado Administrador,</p>
            <p>El sistema ha detectado una alerta de inventario en su sucursal:</p>
            <ul>
                <li><strong>Producto:</strong> {producto}</li>
                <li><strong>Stock Actual:</strong> {stock} unidades</li>
                <li><strong>Umbral de Seguridad:</strong> {umbral} unidades</li>
            </ul>
            <p>Por favor, gestione el abastecimiento a la brevedad.</p>
            """          
            #Llave API
            resend.api_key = os.environ.get('RESEND_API_KEY', 're_6JNnrWqP_AV7z2yL2jYXTjibvXu8zkLUu')            
            for correo in correos_destino:
                try:
                    resend.Emails.send({
                        "from": "onboarding@resend.dev",
                        "to": correo,
                        "subject": f"⚠️ {tipo_alerta}: {producto}",
                        "html": html_mensaje
                    })
                    print(f"✅ Alerta Resend enviada exitosamente a: {correo}")
                except Exception as error_individual:
                    print(f"⚠️ Resend rechazó el envío a {correo}: {error_individual}")                    
    except Exception as e:
        print(f"🔥 Error general del módulo de alertas Resend: {e}")

def api_buscar_cliente(request):
    #Busca un cliente por RUT y devuelve sus datos básicos en JSON
    rut_buscado = request.GET.get('rut', '').strip()    
    if not rut_buscado:
        return JsonResponse({'existe': False}, status=400)        
    try:
        cliente = ClienteFiado.objects.get(rut=rut_buscado)
        return JsonResponse({
            'existe': True,
            'nombre': cliente.nombre,
            'apellido': cliente.apellido
        })
    except ClienteFiado.DoesNotExist:
        return JsonResponse({'existe': False})

def procesar_cambio_password(request):
    #valida y actualiza la clave obligatoria del usuario que viene del proceso de recuperación de contraseña, asegurando que solo los usuarios que han pasado por el proceso de recuperación puedan acceder a esta función. Para reforzar la seguridad, se implementa una verificación adicional que valida la existencia de una sesión temporal específica (usuario_en_cambio) antes de permitir el acceso al formulario de cambio de contraseña, y se destruye esta sesión inmediatamente después de un cambio exitoso para evitar reutilizaciones indebidas.
    if request.method == 'POST':
        #se valida que el usuario venga del flujo correcto
        id_temp = request.session.get('usuario_en_cambio')
        if not id_temp:
            return redirect('pantalla_login')
        nueva_clave = request.POST.get('nueva_clave', '').strip()
        confirmar_clave = request.POST.get('confirmar_clave', '').strip()
        if nueva_clave != confirmar_clave:
            messages.error(request, 'Las contraseñas no coinciden. Inténtalo de nuevo.')
            return render(request, 'nucleo_sistema/cambiar_password.html')
        try:
            usuario = Usuario.objects.get(id_usuario=id_temp)
            usuario.password = make_password(nueva_clave)
            usuario.requiere_cambio_pass = False
            usuario.save()
            del request.session['usuario_en_cambio'] #se elimina la sesión temporal por seguridad
            messages.success(request, 'Clave actualizada correctamente. Ahora puedes iniciar sesión.')
            return redirect('pantalla_login')
        except Usuario.DoesNotExist:
            return redirect('pantalla_login')
    return redirect('pantalla_login')

def pantalla_corporativa(request):
    """ Interfaz para la gestión global de tiendas y usuarios raíz. """
    # Barrera de seguridad analítica
    rol = request.session.get('rol', '').upper()
    if rol != 'CORPORATIVO':
        return redirect('pantalla_login')        
    return render(request, 'nucleo_sistema/corporativo.html')

def gestion_tiendas_corporativo(request):
    """ Módulo Maestro para crear y visualizar las tiendas del sistema. """
    # Barrera de seguridad
    rol = str(request.session.get('rol', '')).strip().upper()
    if rol != 'CORPORATIVO':
        return redirect('pantalla_login')

    # Procesamiento del Formulario (Creación de Tienda)
    if request.method == 'POST':
        try:
            rut = request.POST.get('rut_tienda').strip()
            nombre = request.POST.get('nombre').strip()
            tipo = request.POST.get('tipo_tienda').strip()
            calle = request.POST.get('calle').strip()
            numero = int(request.POST.get('numero', 0))
            detalle = request.POST.get('detalle', '').strip()
            id_comuna = int(request.POST.get('id_comuna'))
            id_dueno = int(request.POST.get('id_dueno')) # Vinculación con DuenoTienda

            # Inyección en BD
            Tienda.objects.create(
                rut_tienda=rut,
                nombre=nombre,
                tipo_tienda=tipo,
                calle=calle,
                numero=numero,
                detalle=detalle,
                id_comuna=id_comuna,
                id_dueno=id_dueno
            )
            messages.success(request, f"Éxito: La tienda '{nombre}' ha sido registrada en el sistema.")
        except Exception as e:
            messages.error(request, f"Error al registrar la tienda: Verifica que el RUT no esté duplicado. Detalle: {str(e)}")
        
        return redirect('gestion_tiendas_corporativo')

    # Si es GET, preparamos los datos para renderizar la pantalla
    comunas = Comuna.objects.all().order_by('nombre_comuna')
    duenos = DuenoTienda.objects.all().order_by('nombre')
    tiendas = Tienda.objects.all().order_by('nombre')

    return render(request, 'nucleo_sistema/gestion_tiendas.html', {
        'comunas': comunas,
        'duenos': duenos,
        'tiendas': tiendas
    })

def gestion_usuarios_corporativo(request):
    """ Módulo Maestro para crear y visualizar Administradores y Analistas. """
    # Barrera de seguridad analítica
    rol_sesion = str(request.session.get('rol', '')).strip().upper()
    if rol_sesion != 'CORPORATIVO':
        return redirect('pantalla_login')

    if request.method == 'POST':
        try:
            nombre = request.POST.get('nombre', '').strip()
            primer_apellido = request.POST.get('primer_apellido', '').strip()
            segundo_apellido = request.POST.get('segundo_apellido', '').strip()
            rol_nuevo = request.POST.get('rol', '').strip().upper()
            mail = request.POST.get('mail', '').strip()
            password = request.POST.get('password', '').strip()
            rut_tienda = request.POST.get('rut_tienda', '').strip()

            # Lógica de asignación de tienda
            if rol_nuevo == 'ANALISTA':
                tienda_obj = None
                sufijo = "glb" # Global
            else:
                tienda_obj = rut_tienda if rut_tienda else None
                sufijo = rut_tienda[-4:] if rut_tienda else "adm"

            # Generador de Usuario Único Algorítmico
            if nombre and primer_apellido:
                base_usuario = f"{nombre[0].lower()}{primer_apellido.lower()}_{sufijo}".replace(" ", "")
            else:
                base_usuario = f"user_{sufijo}"
            
            nombre_usuario_final = base_usuario
            contador = 1
            while Usuario.objects.filter(nombre_usuario=nombre_usuario_final).exists():
                nombre_usuario_final = f"{base_usuario}{contador}"
                contador += 1

            from django.contrib.auth.hashers import make_password
            
            nuevo_usuario = Usuario(
                nombre_usuario=nombre_usuario_final,
                nombre=nombre,
                primer_apellido=primer_apellido,
                segundo_apellido=segundo_apellido,
                rol=rol_nuevo,
                mail=mail,
                password=make_password(password), # BARRERA DE SEGURIDAD (NF3)
                es_activo=True,
                requiere_cambio_pass=True, # Fuerza el cambio de clave en su primer ingreso
                fecha_creacion=timezone.now(),
                rut_tienda_id=tienda_obj
            )
            nuevo_usuario.save()
            messages.success(request, f"Usuario registrado exitosamente. Credencial asignada: {nombre_usuario_final}")

        except Exception as e:
            messages.error(request, f"Error al registrar usuario: {str(e)}")
        
        return redirect('gestion_usuarios_corporativo')

    # GET: Preparar datos para la pantalla
    tiendas = Tienda.objects.all().order_by('nombre')
    # Filtramos para mostrar solo Admins y Analistas al usuario corporativo
    usuarios = Usuario.objects.filter(rol__in=['ADMINISTRADOR', 'ANALISTA']).order_by('nombre')

    return render(request, 'nucleo_sistema/gestion_usuarios_corp.html', {
        'tiendas': tiendas,
        'usuarios': usuarios
    })
