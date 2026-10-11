import yaml
import json
import re
import asyncio
import aiohttp
import requests

CONTROLLER_IP = "192.168.0.10"
PUSHER_URL = "http://{ip}:8080/wm/staticflowpusher/json"
# protocolo -> (número IP en hex, prefijo del campo L4 en el Static Flow Pusher)
PROTO_NUMBERS = {"TCP": ("0x06", "tcp"), "UDP": ("0x11", "udp")}
FLOW_PRIORITY = "32768"

CONNECTIONS = {}      # handler -> datos de la conexión creada
_handler_counter = 0


class Student:
    def __init__(self, name, mac):
        self.name = name
        self.mac = mac


class Service:
    def __init__(self, name, protocol, port):
        self.name = name
        self.protocol = protocol
        self.port = port


class Server:
    def __init__(self, name, ip, services=None):
        self.name = name
        self.ip = ip
        # El YAML entrega dicts; se convierten a objetos Service
        self.services = [Service(s["name"], s["protocol"], s["port"])
                         for s in (services or [])]

    def get_service(self, name):
        for s in self.services:
            if s.name == name:
                return s
        return None


class Course:
    def __init__(self, name, code, state, students, servers):
        self.name = name
        self.code = code
        self.state = state
        self.students = list(students)      # nombres de alumnos
        # {nombre_servidor: [servicios permitidos]}
        self.servers = {s["name"]: list(s["allowed_services"]) for s in servers}

    def add_student(self, student_name):
        if student_name not in self.students:
            self.students.append(student_name)

    def remove_student(self, student_name):
        if student_name in self.students:
            self.students.remove(student_name)

    def add_server(self, server_name, allowed_services):
        """Añade un servidor y sus servicios permitidos al curso."""
        self.servers.setdefault(server_name, [])
        for svc in allowed_services:
            if svc not in self.servers[server_name]:
                self.servers[server_name].append(svc)


def load_data(filename):
    with open(filename, "r") as file:
        data = yaml.safe_load(file)
    students = [Student(x["name"], x["mac"]) for x in data["students"]]
    servers = [Server(y["name"], y["ip"], y["services"]) for y in data["servers"]]
    courses = [Course(z["name"], z["code"], z["state"], z["students"], z["servers"])
               for z in data["courses"]]
    return students, servers, courses


def get_attachment_point(controller_ip, mac):
    endpoint = '/wm/device/'
    url = 'http://' + controller_ip + ':8080' + endpoint
    response = requests.get(
        url,
        params={'mac': mac}
    )
    result, = response.json()
    attachment_points = result.get("attachmentPoint", [])

    if not attachment_points:
        return None, None

    attachment_point, = attachment_points
    return attachment_point["switchDPID"], attachment_point["port"]


def get_route(controller_ip, src_mac, dst_mac):
    src_sw_dpid, src_sw_port = get_attachment_point(controller_ip, src_mac)
    dst_sw_dpid, dst_sw_port = get_attachment_point(controller_ip, dst_mac)
    endpoint = f'/wm/topology/route/{src_sw_dpid}/{src_sw_port}/{dst_sw_dpid}/{dst_sw_port}/json'
    url = 'http://' + controller_ip + ':8080' + endpoint
    response = requests.get(url)
    return response.json()


# --------------------------- Autorización ---------------------------

def courses_with_access(server_name, service_name, courses):
    """Cursos ACTIVE que tienen al servidor y permiten el servicio."""
    return [c for c in courses
            if c.state == "ACTIVE"
            and service_name in c.servers.get(server_name, [])]


def is_authorized(student_name, server_name, service_name, courses):
    """
    Un alumno está autorizado a usar el servicio X del servidor Y si y sólo si
    está matriculado en algún curso que:
      a) tiene estado ACTIVE,
      b) tiene al servidor Y en su lista de servidores,
      c) tiene al servicio X en los servicios permitidos del servidor Y.
    Devuelve (autorizado: bool, curso_que_autoriza | None).
    """
    for course in courses_with_access(server_name, service_name, courses):
        if student_name in course.students:
            return True, course
    return False, None


# ----------------------------- Alumnos -----------------------------

def list_students(students, courses):
    print("\nFiltrar por curso (código, ej. HGW101) o Enter para ver todos:")
    code = input("> ").strip().upper()
    selected = students
    if code:
        course = next((c for c in courses if c.code.upper() == code), None)
        if course is None:
            print(f"No existe el curso {code}")
            return
        selected = [s for s in students if s.name in course.students]
    print(f"\n{'#':<4}{'NOMBRE':<25}MAC")
    print("-" * 55)
    for i, s in enumerate(selected, 1):
        print(f"{i:<4}{s.name:<25}{s.mac}")
    print(f"\nTotal: {len(selected)} alumno(s)")


def create_student(students):
    name = input("Nombre del alumno: ").strip()
    mac = input("Dirección MAC (ej. fa:16:3e:1d:96:70): ").strip().lower()
    if not name:
        print("ERROR: el nombre no puede estar vacío")
        return
    if not re.fullmatch(r"([0-9a-f]{2}:){5}[0-9a-f]{2}", mac):
        print("ERROR: MAC inválida (formato aa:bb:cc:dd:ee:ff)")
        return
    if find_by_name(students, name):
        print("ERROR: ya existe un alumno con ese nombre")
        return
    if any(st.mac.lower() == mac for st in students):
        print("ERROR: esa MAC ya está asignada a otro alumno")
        return
    students.append(Student(name, mac))
    print(f"Alumno '{name}' creado con MAC {mac}")


def show_student(students, courses):
    student = find_by_name(students, input("Nombre del alumno: "))
    if student is None:
        print("ERROR: el alumno no existe")
        return
    enrolled = [c for c in courses if student.name in c.students]
    print(f"\nNombre : {student.name}")
    print(f"MAC    : {student.mac}")
    print("Cursos : " + (", ".join(f"{c.code} ({c.state})" for c in enrolled)
                         if enrolled else "(ninguno)"))


def students_menu(students, courses):
    while True:
        print("\n--- ALUMNOS ---")
        print("1. Crear")
        print("2. Listar")
        print("3. Mostrar detalles")
        print("4. Actualizar")
        print("5. Borrar")
        print("6. Volver")
        op = input("> ").strip()
        if op == "1":
            create_student(students)
        elif op == "2":
            list_students(students, courses)
        elif op == "3":
            show_student(students, courses)
        elif op == "6":
            return
        else:
            print("Opción aún no implementada")


# ------------------------------- Cursos -------------------------------

def find_course(courses, code):
    return next((c for c in courses if c.code.upper() == code.strip().upper()), None)


def list_courses(courses):
    print(f"\n{'CÓDIGO':<9}{'NOMBRE':<32}{'ESTADO':<10}ALUMNOS")
    print("-" * 60)
    for c in courses:
        print(f"{c.code:<9}{c.name:<32}{c.state:<10}{len(c.students)}")


def show_course(courses, students):
    course = find_course(courses, input("Código del curso (ej. HGW101): "))
    if course is None:
        print("ERROR: el curso no existe")
        return
    print(f"\nCurso  : {course.name} ({course.code})")
    print(f"Estado : {course.state}")
    print("Alumnos:")
    if not course.students:
        print("  (ninguno)")
    for n in course.students:
        st = find_by_name(students, n)
        print(f"  - {n}  [{st.mac if st else 'sin MAC'}]")
    print("Servidores y servicios permitidos:")
    if not course.servers:
        print("  (ninguno)")
    for srv, svcs in course.servers.items():
        print(f"  - {srv}: {', '.join(svcs)}")


def manage_course_students(courses, students):
    course = find_course(courses, input("Código del curso: "))
    if course is None:
        print("ERROR: el curso no existe")
        return
    print("1. Agregar alumno")
    print("2. Eliminar alumno")
    op = input("> ").strip()
    if op not in ("1", "2"):
        print("Opción inválida")
        return
    student = find_by_name(students, input("Nombre del alumno: "))
    if student is None:
        print("ERROR: el alumno no existe")
        return
    if op == "1":
        if student.name in course.students:
            print(f"{student.name} ya está matriculado en {course.code}")
            return
        course.add_student(student.name)
        print(f"{student.name} agregado al curso {course.code}")
    else:
        if student.name not in course.students:
            print(f"{student.name} no está matriculado en {course.code}")
            return
        course.remove_student(student.name)
        print(f"{student.name} eliminado del curso {course.code}")


def courses_menu(courses, students):
    while True:
        print("\n--- CURSOS ---")
        print("1. Crear")
        print("2. Listar")
        print("3. Mostrar detalles")
        print("4. Actualizar")
        print("5. Gestionar alumnos")
        print("6. Borrar")
        print("7. Volver")
        op = input("> ").strip()
        if op == "2":
            list_courses(courses)
        elif op == "3":
            show_course(courses, students)
        elif op == "5":
            manage_course_students(courses, students)
        elif op == "7":
            return
        else:
            print("Opción aún no implementada")


# ----------------------------- Servidores -----------------------------

def list_servers(servers):
    print(f"\n{'#':<4}{'NOMBRE':<14}IP")
    print("-" * 34)
    for i, sv in enumerate(servers, 1):
        print(f"{i:<4}{sv.name:<14}{sv.ip}")


def show_server_services(servers, courses):
    server = find_by_name(servers, input("Nombre del servidor (ej. Server 3): "))
    if server is None:
        print("ERROR: el servidor no existe")
        return
    print(f"\nServidor: {server.name}   IP: {server.ip}")
    print(f"{'SERVICIO':<10}{'PROTO':<7}{'PUERTO':<8}CURSOS CON ACCESO")
    print("-" * 70)
    for svc in server.services:
        granted = [f"{c.code} ({c.state})" for c in courses
                   if svc.name in c.servers.get(server.name, [])]
        print(f"{svc.name:<10}{svc.protocol:<7}{svc.port:<8}"
              f"{', '.join(granted) if granted else '-'}")


def servers_menu(servers, courses):
    while True:
        print("\n--- SERVIDORES Y SERVICIOS ---")
        print("1. Crear")
        print("2. Listar")
        print("3. Mostrar servicios")
        print("4. Actualizar")
        print("5. Borrar")
        print("6. Volver")
        op = input("> ").strip()
        if op == "2":
            list_servers(servers)
        elif op == "3":
            show_server_services(servers, courses)
        elif op == "6":
            return
        else:
            print("Opción aún no implementada")


# ------------------------------ Conexiones ------------------------------

def find_by_name(items, name):
    return next((x for x in items if x.name.lower() == name.strip().lower()), None)


def create_connection(students, servers, courses):
    global _handler_counter
    student = find_by_name(students, input("Alumno: "))
    if student is None:
        print("ERROR: el alumno no existe")
        return
    server = find_by_name(servers, input("Servidor: "))
    if server is None:
        print("ERROR: el servidor no existe")
        return
    service = server.get_service(input("Servicio (ej. ssh): ").strip())
    if service is None:
        print("ERROR: el servidor no ofrece ese servicio")
        return

    ok, course = is_authorized(student.name, server.name, service.name, courses)
    if not ok:
        print(f"ERROR: {student.name} NO está autorizado a usar "
              f"{service.name} en {server.name}")
        return

    try:
        src_dev = get_device(CONTROLLER_IP, mac=student.mac)
        dst_dev = get_device(CONTROLLER_IP, ipv4=server.ip)
        if not src_dev or not src_dev["ipv4"]:
            print("ERROR: el controlador aún no conoce la IP del alumno "
                  "(genera tráfico ARP desde su host)")
            return
        if not dst_dev:
            print("ERROR: el controlador aún no conoce al servidor "
                  "(genera tráfico ARP desde el servidor)")
            return
        src_ip, dst_mac = src_dev["ipv4"][0], dst_dev["mac"][0]
        route = get_route(CONTROLLER_IP, student.mac, dst_mac)
    except Exception as e:
        print(f"ERROR consultando al controlador: {e}")
        return
    if not route:
        print("ERROR: no hay ruta entre el alumno y el servidor")
        return

    _handler_counter += 1
    handler = f"C{_handler_counter}"
    flows = build_route(CONTROLLER_IP, route, student.mac, dst_mac, src_ip,
                        server.ip, service.protocol, service.port, handler)
    if flows is None:
        print("ERROR: no se pudieron insertar las flow entries")
        return
    CONNECTIONS[handler] = {"student": student.name, "server": server.name,
                            "service": service.name, "flows": flows}
    print(f"Conexión creada (autorizada por {course.code}). "
          f"Handler: {handler} - {len(flows)} flows en "
          f"{len(set(f['switch'] for f in flows))} switches")


def list_connections():
    if not CONNECTIONS:
        print("No hay conexiones creadas manualmente")
        return
    print(f"\n{'HANDLER':<9}{'ALUMNO':<22}{'SERVIDOR':<12}{'SERVICIO':<10}FLOWS")
    print("-" * 60)
    for h, c in CONNECTIONS.items():
        print(f"{h:<9}{c['student']:<22}{c['server']:<12}{c['service']:<10}"
              f"{len(c['flows'])}")


def delete_connection(students, servers, courses):
    handler = input("Handler de la conexión: ").strip().upper()
    conn = CONNECTIONS.get(handler)
    if conn is None:
        print("ERROR: no existe esa conexión")
        return
    ok, _ = is_authorized(conn["student"], conn["server"], conn["service"], courses)
    if not ok:
        print(f"ERROR: {conn['student']} NO está autorizado; no se elimina")
        return
    if delete_flows(CONTROLLER_IP, [f["name"] for f in conn["flows"]]):
        del CONNECTIONS[handler]
        print(f"Conexión {handler} eliminada")
    else:
        print("ERROR: no se pudieron eliminar todas las flow entries")


def connections_menu(students, servers, courses):
    while True:
        print("\n--- CONEXIONES ---")
        print("1. Crear")
        print("2. Listar")
        print("3. Mostrar detalles")
        print("4. Recalcular")
        print("5. Actualizar")
        print("6. Borrar")
        print("7. Volver")
        op = input("> ").strip()
        if op == "1":
            create_connection(students, servers, courses)
        elif op == "2":
            list_connections()
        elif op == "6":
            delete_connection(students, servers, courses)
        elif op == "7":
            return
        else:
            print("Opción aún no implementada")


# ------------------------------- Menú -------------------------------

def menu(students, servers, courses):
    while True:
        print("\n=========== MENÚ PRINCIPAL ===========")
        print("1. Importar")
        print("2. Exportar")
        print("3. Cursos")
        print("4. Alumnos")
        print("5. Servidores")
        print("6. Políticas")
        print("7. Conexiones")
        print("8. Salir")
        op = input("> ").strip()
        if op == "3":
            courses_menu(courses, students)
        elif op == "4":
            students_menu(students, courses)
        elif op == "5":
            servers_menu(servers, courses)
        elif op == "7":
            connections_menu(students, servers, courses)
        elif op == "8":
            print("Saliendo...")
            return
        else:
            print("Opción aún no implementada")


# ------------------------- Rutas y flow entries -------------------------

def get_device(controller_ip, mac=None, ipv4=None):
    """Consulta /wm/device/ por MAC o IPv4. Devuelve el dispositivo o None."""
    params = {}
    if mac:
        params["mac"] = mac
    if ipv4:
        params["ipv4"] = ipv4
    response = requests.get(f"http://{controller_ip}:8080/wm/device/",
                            params=params, timeout=5)
    devices = response.json()
    return devices[0] if devices else None


def route_hops(route):
    """
    /wm/topology/route devuelve una lista plana de (switch, puerto):
    por cada switch vienen dos entradas consecutivas, [puerto de entrada,
    puerto de salida]. Se agrupan en tuplas (dpid, in_port, out_port).
    """
    hops = []
    for i in range(0, len(route) - 1, 2):
        hops.append((route[i]["switch"],
                     route[i]["port"]["portNumber"],
                     route[i + 1]["port"]["portNumber"]))
    return hops


def build_flows(route, src_mac, dst_mac, src_ip, dst_ip, proto, port, handler):
    """
    Genera (sin enviar) las flow entries de una conexión alumno <-> servicio.
    Por cada switch de la ruta:
      - IP/L4 alumno -> servidor  (L2 + L3 + L4, sólo el puerto del servicio)
      - IP/L4 servidor -> alumno  (sentido inverso, puerto origen = servicio)
      - ARP alumno -> servidor y ARP servidor -> alumno
    """
    ip_proto, l4 = PROTO_NUMBERS[proto.upper()]
    flows = []
    for n, (sw, p_in, p_out) in enumerate(route_hops(route), 1):
        common = {"switch": sw, "active": "true", "priority": FLOW_PRIORITY}
        flows += [
            {**common, "name": f"{handler}-s{n}-fwd", "in_port": str(p_in),
             "eth_type": "0x0800", "eth_src": src_mac, "eth_dst": dst_mac,
             "ipv4_src": src_ip, "ipv4_dst": dst_ip, "ip_proto": ip_proto,
             f"{l4}_dst": str(port), "actions": f"output={p_out}"},
            {**common, "name": f"{handler}-s{n}-rev", "in_port": str(p_out),
             "eth_type": "0x0800", "eth_src": dst_mac, "eth_dst": src_mac,
             "ipv4_src": dst_ip, "ipv4_dst": src_ip, "ip_proto": ip_proto,
             f"{l4}_src": str(port), "actions": f"output={p_in}"},
            {**common, "name": f"{handler}-s{n}-arpf", "in_port": str(p_in),
             "eth_type": "0x0806", "eth_src": src_mac, "arp_tpa": dst_ip,
             "actions": f"output={p_out}"},
            {**common, "name": f"{handler}-s{n}-arpr", "in_port": str(p_out),
             "eth_type": "0x0806", "eth_src": dst_mac, "arp_tpa": src_ip,
             "actions": f"output={p_in}"},
        ]
    return flows


async def _push_flow(session, url, flow):
    async with session.post(url, data=json.dumps(flow)) as resp:
        body = await resp.json(content_type=None)
        return flow["name"], resp.status, body


async def _delete_flow(session, url, name):
    async with session.delete(url, data=json.dumps({"name": name})) as resp:
        body = await resp.json(content_type=None)
        return name, resp.status, body


async def _run_all(coros_factory):
    timeout = aiohttp.ClientTimeout(total=10)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        return await asyncio.gather(*coros_factory(session), return_exceptions=True)


def _all_ok(results):
    return all(isinstance(r, tuple) and r[1] == 200 for r in results)


def delete_flows(controller_ip, flow_names):
    """Borra flow entries por nombre (de forma asíncrona)."""
    url = PUSHER_URL.format(ip=controller_ip)
    results = asyncio.run(_run_all(
        lambda sess: [_delete_flow(sess, url, n) for n in flow_names]))
    return _all_ok(results)


def build_route(controller_ip, route, src_mac, dst_mac, src_ip, dst_ip,
                proto, port, handler):
    """
    Inserta de forma asíncrona (asyncio + aiohttp) todas las flow entries
    necesarias para habilitar la conectividad alumno <-> servicio.
    Devuelve la lista de flows insertados o None si alguna falló (en ese caso
    se hace rollback de las que sí se insertaron).
    """
    flows = build_flows(route, src_mac, dst_mac, src_ip, dst_ip,
                        proto, port, handler)
    url = PUSHER_URL.format(ip=controller_ip)
    results = asyncio.run(_run_all(
        lambda sess: [_push_flow(sess, url, f) for f in flows]))
    if not _all_ok(results):
        for r in results:
            print("  fallo:", r)
        delete_flows(controller_ip, [f["name"] for f in flows])
        return None
    return flows


def main():
    students, servers, courses = load_data("database.yaml")
    menu(students, servers, courses)


if __name__ == "__main__":
    main()
