#from application import app, error_queue
from flask import Response, request
import kombu
import json
import logging
import traceback
import threading
import requests
import operator
import re
from datetime import datetime


app_config = None
final_log = []
error_queue = None


def is_running():
    threads = [t for t in threading.enumerate() if t.name == 'migrate_thread']
    s = True if len(threads) > 0 and threads[0].is_alive() else False
    return s


def start_migration():
    t = threading.Thread(name='migrate_thread', target=migration_thread)
    t.daemon = False
    t.start()


class MigrationException(RuntimeError):
    def __init__(self, message, text=None):
        super(RuntimeError, self).__init__(message)
        self.text = text


def get_registrations_to_migrate(start_date, end_date):
    url = app_config['B2B_LEGACY_URL'] + '/land_charges/' + start_date + '/' + end_date
    headers = {'Content-Type': 'application/json'}
    logging.info("GET %s", url)
    response = requests.get(url, headers=headers, params={'type': 'NR'})
    logging.info("Responses: %d", response.status_code)
    
    if response.status_code == 200:
        list = response.json()
        logging.info("Found %d items", len(list))
        return list
    else:
        raise MigrationException("Unexpected response {} from {}", response.status_code, url)



# TODO: Important! Can we have duplicate rows on T_LC_DOC_INFO with matching reg number and date???

def get_doc_history(reg_no, class_of_charge, date):
    url = app_config['B2B_LEGACY_URL'] + '/doc_history/' + reg_no
    headers = {'Content-Type': 'application/json'}
    logging.info("  GET %s?class=%s&date=%s", url, class_without_brackets(class_of_charge), date)
    response = requests.get(url, headers=headers, params={'class': class_without_brackets(class_of_charge), 'date': date})
    logging.info('  Response: %d', response.status_code)
    
    if response.status_code != 200:
        logging.warning("Non-200 return code {} for {}".format(response.status_code, url))

    if response.status_code == 404:
        return None

    return response.json()


def get_land_charge(reg_no, class_of_charge, date):
    url = app_config['B2B_LEGACY_URL'] + '/land_charges/' + str(reg_no)
    headers = {'Content-Type': 'application/json'}
    logging.info('    GET %s?class=%s&date=%s', url, class_of_charge, date)
    response = requests.get(url, headers=headers, params={'class': class_of_charge, 'date': date})
    logging.info('    Response: %d', response.status_code)
    if response.status_code == 200:
        return response.json()
    elif response.status_code == 404:
        return None
    else:
        raise MigrationException("Unexpected response {} from {}".format(response.status_code, url),
                                 response.text)


def add_flag(data, flag):
    for item in data:
        item['migration_data']['flags'].append(flag)


def flag_oddities(data):
    # There aren't many circumstances when we can't migrate something - often source data consists only
    # of registration number, date and class of charge (i.e. rest of the data is in the form image)
    # Flag the oddities up anyway so we can check out any data quality issues

    if data[0]['type'] != 'NR':
        add_flag(data, "Does not start with NR")

    for item in data:
        if item['type'] == 'NR':
            if item != data[0]:
                add_flag(data, "NR is not the first item")
            # if item['migration_data']['original']['registration_no'] != item['registration']['registration_no'] or \
               # item['migration_data']['original']['date'] != item['registration']['date']:
                # add_flag(data, "NR has inconsitent original details")
    
    if 'eo_name' not in data[-1]:
        add_flag(data, "Last item lacks name information")
                

def log_item_summary(data):
    global final_log
    for item in data:
        final_log.append("Processed " + item['registration']["date"] + "/" + str(item['registration']['registration_no']))
        for flag in item['migration_data']['flags']:
            final_log.append("  " + flag)
        

def migrate(config, start, end):
    global app_config
    global error_queue
    app_config = config
    
    hostname = "amqp://{}:{}@{}:{}".format(app_config['MQ_USERNAME'], app_config['MQ_PASSWORD'],
                                           app_config['MQ_HOSTNAME'], app_config['MQ_PORT'])
    connection = kombu.Connection(hostname=hostname)
    error_queue = connection.SimpleQueue('errors')

    logging.info('Migration started')
    error_count = 0
    # Get all the registration numbers that need to be migrated

    try:
        reg_data = get_registrations_to_migrate(start, end)
        if not isinstance(reg_data, list):
            msg = "Registration data is not a list:"
            logging.error(msg)
            logging.error(reg_data)
            report_error("E", msg, json.dumps(reg_data))
            return

        total_read = len(reg_data)
        logging.info("Retrieved %d items from /land_charges", total_read)

    except Exception as e:
        logging.error('Unhandled exception: %s', str(e))
        report_exception(e)
        raise

    total_inc_history = 0
    for rows in reg_data:
        try:
            # For each registration number returned, get history of that application.
            logging.info("------------------------------------------------------------------")
            rows['class'] = rows['class'].strip()
            rows['reg_no'] = rows['reg_no'].strip()
            rows['date'] = rows['date'].strip()
            logging.info("Process %s %s/%s", rows['class'], rows['date'], rows['reg_no'])

            history = get_doc_history(rows['reg_no'], rows['class'], rows['date'])
            if history is None or len(history) == 0:
                logging.error("  No document history information found") # TODO: need a bucket of these
                continue

            total_inc_history += len(history)
            for i in history:
                i['sorted_date'] = datetime.strptime(i['date'], '%Y-%m-%d').date()
                #i['reg_no'] = int(i['reg_no'])
            
            logging.info("  Chain of length %d found", len(history))
            history.sort(key=operator.itemgetter('sorted_date', 'reg_no'))
            registration = []
            
            for x, registers in enumerate(history):
                registers['class'] = convert_class(registers['class'])
                
                
                logging.info("    Historical record %s %s %s", registers['class'], registers['reg_no'],
                             registers['date'])

                numeric_reg_no = int(re.sub("/", "", registers['reg_no'])) # TODO: is this safe?
                land_charges = get_land_charge(numeric_reg_no, registers['class'], registers['date'])
                
                if land_charges is not None and len(land_charges) > 0:
                    registration.append(extract_data(land_charges, registers['type']))
                    registration[x]['reg_no'] = numeric_reg_no
                    
                else:
                    del registers['sorted_date']
                    registers['registration'] = {
                        'registration_no': numeric_reg_no,
                        'date': registers['date']
                    }
                    registers['class_of_charge'] = registers['class']
                    registers['application_ref'] = ' '
                    registers['migration_data'] = {
                        'unconverted_reg_no': registers['reg_no'],
                        'flags': [],
                        'original': {
                            'registration_no': registers['orig_number'],
                            'date': registers['orig_date'],
                            'class': registers['orig_class']
                        }
                    }
                    registers['residence'] = {"text": ""}
                    registration.append(registers)

            flag_oddities(registration)
            
            registration_status_code = insert_data(registration)
            if registration_status_code != 200:
                url = app_config['BANKRUPTCY_DATABASE_API'] + '/migrated_record'
                message = "Unexpected {} return code for POST {}".format(registration_status_code, url)
                logging.error("  " + message)
                report_error("E", message, "")

                logging.error("Rows:")
                logging.error(rows)
                logging.error("Registration:")
                logging.error(registration)
                error_count += 1
                item = registration[0]
                final_log.append('Failed to migrate' + item['registration']["date"] + "/" + str(item['registration']['registration_no']))
            else:
                log_item_summary(registration)
                
            #final_log.append
        except Exception as e:
            logging.error('Unhandled exception: %s', str(e))
            logging.error('Failed to migrate  %s %s %s', rows['class'], rows['reg_no'], rows['date'])
            report_exception(e)
            error_count += 1

    logging.info('Migration complete')
    logging.info("Total registrations read: %d", total_read)
    logging.info("Total records processed: %d", total_inc_history)
    logging.info("Total errors: %d", error_count)
    
    for line in final_log:
        logging.info(line)


def report_exception(exception):
    global error_queue
    call_stack = traceback.format_exc()
    logging.error(call_stack)
    
    error = {
        "type": "E",
        "message": str(exception),
        "stack": call_stack,
        "subsystem": app_config["APPLICATION_NAME"]
    }
    # TODO: also report exception.text
    error_queue.put(error)


def report_error(error_type, message, stack):
    global error_queue
    error = {
        "type": error_type,
        "message": message,
        "subsystem": app_config["APPLICATION_NAME"],
        "stack": stack
    }
    error_queue.put(error)



# For testing error queueing:
# @app.route('/force_error', methods=['POST'])
# def force_error():
    # report_error("I", "Test Error", "Stack goes here")
    # return Response(status=200)


def extract_data(rows, app_type):
    #print(rows)
    data = rows[0]

    if data['reverse_name_hex'][-2:] == '01':
        # County council
        logging.info('      EO Name is County Council')
        registration = build_registration(data, 'County Council', {'local': {'name': data['name'], 'area': '?????'}})        
    elif data['reverse_name_hex'][-2:] == '02':
        # Rural council
        logging.info('      EO Name is Rural Council')
        registration = build_registration(data, 'Rural Council', {'local': {'name': data['name'], 'area': '?????'}})
    elif data['reverse_name_hex'][-2:] == '04':
        # Parish council
        logging.info('      EO Name is Parish Council')
        registration = build_registration(data, 'Parish Council', {'local': {'name': data['name'], 'area': '?????'}})
    elif data['reverse_name_hex'][-2:] == '08':
        # Other council
        logging.info('      EO Name is Other Council')
        registration = build_registration(data, 'Other Council', {'local': {'name': data['name'], 'area': '?????'}})
    elif data['reverse_name_hex'][-2:] == '16':
        # Dev corp
        logging.info('      EO Name is Development Corporation')
        registration = build_registration(data, 'Development Corporation', {'other': data['name']})
    elif data['reverse_name_hex'][-2:] == 'F1':
        # Ltd Company
        logging.info('      EO Name is Limited Company')
        registration = build_registration(data, 'Limited Company', {'company': data['name']})
    elif data['reverse_name_hex'][-2:] == 'F2':
        # Other
        logging.info('      EO Name is Other')
        registration = build_registration(data, 'Other', {'other': data['name']})
    elif data['reverse_name_hex'][-2:] == 'F3' and data['reverse_name_hex'][0:2] == 'F9':
        logging.info('      EO Name is Complex Name')
        registration = build_registration(data, 'Complex Name', {'complex': {'name': data['name'], 'number': int(data['reverse_name'][2:8], 16)}})
    else:    
        # Mundane name
        logging.info('      EO Name is Simple')
        registration = extract_simple(data)
    
    registration['type'] = app_type
    return registration

    # data = rows[0]

    # # determine the type of extraction needed - simple name/complex name/local authority
    # logging.info("HEX: " + data['reverse_name_hex'])
    # if data['reverse_name_hex'][0:2] == 'F9':  # TODO: is this right? Isn't the cnum at the end of the string

    # elif data['name'] != "":
        
    # else:
        

    


def extract_simple(rows):
    hex_codes = []
    length = len(rows['punctuation_code'])
    count = 0
    while count < length:
        hex_codes.append(rows['punctuation_code'][count:(count + 2)])
        count += 2

    orig_name = rows["remainder_name"] + rows["reverse_name"][::-1]
    name_list = []
    for items in hex_codes:
        punc, pos = hex_translator(items)
        name_list.append(orig_name[:pos])
        name_list.append(punc)
        orig_name = orig_name[pos:]

    name_list.append(orig_name)
    full_name = ''.join(name_list)
    try:
        surname_pos = full_name.index('*')
        forenames = full_name[:surname_pos]
        surname = full_name[surname_pos + 1:]
    except ValueError:
        surname = ""
        forenames = full_name

    forenames = forenames.split()

    registration = build_registration(rows, 'Private Individual', {'private': {'forenames': forenames, 'surname': surname}})
    return registration


#def build_registration(rows, forenames=None, surname=None, complex_data=None):
def build_registration(rows, name_type, name_data):

    registration = {
        "class_of_charge": rows['class_type'],
        "application_ref": rows['amendment_info'],
        "registration": {
            "date": rows['registration_date'],
            "registration_no": rows['registration_no']
        },
        "date": rows['registration_date'],  # TODO: find actual date of appn
        "occupation": "",
        "residence": {"text": rows['address']},
        "migration_data": {
            "registration_no": rows['registration_no'],
            'unconverted_reg_no': rows['registration_no'],
            'flags': [],
            "extra": {
                "occupation": rows['occupation'],
                "counties": rows['counties'],
                "property": rows['property'],
                "parish_district": rows['parish_district'],
                "priority_notice_ref": rows['priority_notice_ref']
            }
        }
    }
    
    
    
    
    
    #if registration['class_of_charge'] in ['PA(B)', 'WO(B)']:
    registration['eo_name'] = name_data
    registration['eo_name']['estate_owner_ind'] = name_type
    
    # Add the remaining empty name options
    if not 'local' in registration['eo_name']:
        registration['eo_name']['local'] = {'name': None, 'area': None}
    if not 'company' in registration['eo_name']:    
        registration['eo_name']['company'] = None
    if not 'other' in registration['eo_name']:
        registration['eo_name']['other'] = None
    if not 'complex' in registration['eo_name']:
        registration['eo_name']['complex'] = {'name': None, 'number': 0}
    if not 'private' in registration['eo_name']:
        registration['eo_name']['private'] = {'forenames': [], 'surname': ''}

    
        # if complex_data is None:
            # registration['eo_name'] = [{"forenames": forenames, "surname": surname}]
        # else:

            # registration['eo_name'] = [{"forenames": [""], "surname": ""}]       
    # else:
        # registration[


    return registration


def insert_data(registration):
    json_data = registration
    url = app_config['BANKRUPTCY_DATABASE_API'] + '/migrated_record'
    headers = {'Content-Type': 'application/json'}
    logging.info("  POST %s", url)
    response = requests.post(url, data=json.dumps(json_data), headers=headers)
    logging.info("  Response: %d", response.status_code)
    
    registration_status_code = response.status_code
    # add code below to force errors
    # registration_status_code = 500
    return registration_status_code


def hex_translator(hex_code):
    compare_bit = 0x1F
    compare_int = int(compare_bit)
    myint = int(hex_code, 16)
    int_3 = myint >> 5
    bit_3 = bin(int_3)
    diff = compare_int & myint
    diff_bit = (bin(diff))
    dec_5 = int(diff_bit, 2)
    punctuation = {
        "0b1": " ",
        "0b10": "-",
        "0b11": "'",
        "0b100": "(",
        "0b101": ")",
        "0b110": "*",
        "0b0": "&"
    }

    return punctuation[str(bit_3)], dec_5


def convert_class(class_of_charge):
    charge = {
        "C1": "C(I)",
        "C2": "C(II)",
        "C3": "C(III)",
        "C4": "C(IV)",
        "D1": "D(I)",
        "D2": "D(II)",
        "D3": "D(III)",
        "PAB": "PA(B)",
        "WOB": "WO(B)"
    }
    if class_of_charge in charge:
        return charge.get(class_of_charge)
    else:
        return class_of_charge

        
        
def class_without_brackets(class_of_charge):
    charge = {
        "C(I)": "C1",
        "C(II)": "C2",
        "C(III)": "C3",
        "C(IV)": "C4",
        "D(I)": "D1",
        "D(II)": "D2",
        "D(III)": "D3",
        "PA(B)": "PAB",
        "WO(B)": "WOB"
    }
    if class_of_charge in charge:
        return charge.get(class_of_charge)
    else:
        return class_of_charge



def extract_address(address):
    marker = "   "
    address_list = []
    address_1 = {
        "text": ""
    }

    try:
        marker_pos = address.index(marker)
    except ValueError:
        address_1['text'] = address
        address_list.append(address_1.copy())
        return address_list

    while marker_pos > 0:
        address_1['text'] = address[:marker_pos]
        address = address[marker_pos + 3:]
        address_list.append(address_1.copy())
        try:
            marker_pos = address.index(marker)
        except ValueError:
            address_1['text'] = address
            marker_pos = 0
            address_list.append(address_1.copy())
    return address_list
