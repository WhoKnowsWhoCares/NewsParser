// db.log.insertOne({"message": "Database created."});

// db.getSiblingDB('admin').auth(
//     process.env.MONGO_INITDB_ROOT_USERNAME,
//     process.env.MONGO_INITDB_ROOT_PASSWORD
// );

// db = db.getSiblingDB('admin'); 
// db.createUser(
//     {
//         user: '$DB_USER',
//         pwd: '$DB_PASSWORD',
//         roles: [ 
//             { role: "userAdminAnyDatabase", db: "admin" },
//             { role: "readWriteAnyDatabase", db: "admin" },
//             { role: "dbAdminAnyDatabase", db: "admin" }
//         ]
//     }
// );

// db.auth('$MONGO_INITDB_ROOT_USERNAME', '$MONGO_INITDB_ROOT_PASSWORD'); 
// db = db.getSiblingDB('$DB_NAME'); 
// db.createUser(
//     { 
//         user: '$DB_USER', 
//         pwd: '$DB_PASSWORD', 
//         roles: [{ role: 'readWrite', db: '$DB_NAME' }] 
//     }
// );